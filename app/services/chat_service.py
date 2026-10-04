import logging
import time
from dataclasses import dataclass
from uuid import UUID

from fastapi import WebSocket
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from app.models.Attachment import Attachment
from app.models.Channel import Channel
from app.models.Conversation import Conversation
from app.models.ForumPost import ForumPost
from app.models.Message import Message
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.permissions import Permission
from app.services import forum, read_state
from app.services.message_content import InvalidContent, normalize_content, serialize
from app.services.connection_manager import ConnectionManager
from app.services.permissions import permissions
from app.services.push import (
    channel_tag, dm_payload, dm_tag, mention_payload, mentioned_user_ids, plain_text, push)
from app.services.replies import reply_json, reply_refs
from app.ws_schemas import DirectMessageFrame, MessageFrame, TypingFrame

logger = logging.getLogger("app.services.chat_service")


_INVALID_REPLY = object()

TYPING_MIN_INTERVAL = 2.0
_TYPING_PRUNE_THRESHOLD = 1024


class _AttachmentsUnavailable(Exception):
    """An attachment was linked to another message between the check and the
    write; the message must not be stored."""


class MessageRejected(Exception):
    """The message was not stored; *code* is the error code for the sender."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MessageAlreadyStored(Exception):
    """The sender already stored a message with this id."""


@dataclass
class CreatedMessage:
    message: Message
    frame: dict
    post_row: dict | None


class ChatService:
    """Handles incoming chat messages: persists to DB and broadcasts to server members."""

    clock = staticmethod(time.monotonic)

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self.connection_manager = connection_manager
        self._typing_accepted: dict[tuple[int, tuple], float] = {}

    async def handle_message(
        self, sender_id: int, message: MessageFrame, sender_ws: WebSocket
    ) -> None:
        """Persist a validated chat message from *sender_id* and fan it out.

        *sender_id* is the authenticated owner of the socket. The frame has
        already been validated (see ws_schemas); any ``user_id`` the client put
        in it is discarded.
        """
        try:
            await self.create_channel_message(
                sender_id, message, sender_ws=sender_ws)
        except MessageAlreadyStored:
            await self._send_ack(sender_ws, message.id)
        except MessageRejected as rejected:
            await self._send_error(sender_ws, rejected.code, message.id)
        else:
            await self._send_ack(sender_ws, message.id)

    async def create_channel_message(
        self,
        sender_id: int,
        fields: MessageFrame,
        *,
        post: forum.NewPost | None = None,
        sender_ws: WebSocket | None = None,
        skip_sender: bool = False,
    ) -> CreatedMessage:
        """Everything after the frame is parsed: access, the forum post rule,
        content, attachments, quote, persist, read state, fan-out and mention
        push. Shared by the socket and the post-create endpoint.

        *post* makes this the opening message of a new post in a forum
        channel; otherwise ``fields.post_id`` names the post of a reply.
        Messages go to the channel's members; the sending socket
        (*sender_ws*), or with *skip_sender* all of the sender's sockets, is
        left out. Raises MessageRejected, or MessageAlreadyStored when this
        sender already stored this id.
        """
        server_id = fields.server_id
        channel_id = fields.channel_id

        # Being logged in is not enough: the sender has to be allowed to post
        # in the server, and the channel has to actually live in that server.
        can_send = await permissions.has(
            sender_id, server_id, Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES)
        channel = can_send and await Channel.get_or_none(id=channel_id, server_id=server_id)
        if not channel:
            logger.warning(
                "User %s tried to post to server %s / channel %s without access",
                sender_id, server_id, channel_id,
            )
            raise MessageRejected("forbidden")

        await self._reject_stored(sender_id, fields.id, channel_id=channel_id)
        reply_post = await self._check_post(sender_id, channel, fields.post_id, post)

        try:
            content = normalize_content(
                fields.content, allow_empty=bool(fields.attachment_ids))
        except InvalidContent:
            raise MessageRejected("invalid_content")

        attachments = await self._load_attachments(
            sender_id, fields.attachment_ids, channel_id=channel_id)
        if attachments is None:
            raise MessageRejected("invalid_attachments")

        reply_to_uuid = await self._resolve_reply(
            fields.reply_to, channel_id=channel_id, post_id=fields.post_id)
        if reply_to_uuid is _INVALID_REPLY:
            raise MessageRejected("invalid_reply")

        # Persist first: never show other people a message that was not stored.
        try:
            created = await self._create_message(
                attachments,
                new_post=post,
                reply_post=reply_post,
                uuid=fields.id,
                content=serialize(content),
                author_id=sender_id,
                server_id=server_id,
                channel_id=channel_id,
                timestamp=fields.timestamp,
                metadata=self._stored_metadata(fields),
                reply_to_uuid=reply_to_uuid,
            )
        except IntegrityError:
            # A concurrent send of the same id won the unique constraint.
            await self._reject_stored(sender_id, fields.id, channel_id=channel_id)
            raise
        if created is None:
            raise MessageRejected("invalid_attachments")
        post_row = None
        if created.post_id is not None:
            stored_post = await ForumPost.get(id=created.post_id)
            post_row = (await forum.rows([stored_post]))[0]

        # Your own message can never leave the thread unread for you, even
        # after a reload on another device.
        await read_state.advance(
            sender_id, channel_id=channel_id, message_pk=created.id)

        # Rebuild the frame from known fields instead of relaying the client's
        # dict, so the sender can't smuggle a forged user_id (or anything else)
        # to other clients.
        refs = await reply_refs([reply_to_uuid])
        outgoing = {
            "type": "message",
            "id": fields.id,
            "server_id": server_id,
            "channel_id": channel_id,
            "post_id": created.post_id,
            "user_id": sender_id,
            "content": content,
            "timestamp": fields.timestamp,
            "attachments": [a.to_json() for a in attachments],
            "reactions": [],
            "embeds": [e.model_dump() for e in fields.embeds],
            "reply_to": reply_json(reply_to_uuid, refs),
        }

        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)

        exclude_user_id = sender_id if skip_sender else None
        if post is not None:
            await self._fan_out(
                forum.created_frame(server_id, post_row), member_ids,
                exclude_user_id=exclude_user_id)
        await self._fan_out(
            outgoing, member_ids, sender_ws=sender_ws, exclude_user_id=exclude_user_id)
        if post is None and post_row is not None:
            # The sender's socket only got an ack, so it needs this too.
            await self._fan_out(forum.updated_frame(server_id, post_row), member_ids)

        if push.enabled:
            # Runs in the sender's socket path: a push problem must never
            # break message delivery.
            try:
                mentioned = mentioned_user_ids(content)
                if mentioned:
                    push.schedule(self._push_mentions(
                        sender_id, server_id, channel_id, created.post_id, created.id,
                        str(created.uuid), content, attachments, mentioned, member_ids))
            except Exception:
                logger.warning("Failed to schedule mention pushes", exc_info=True)
        return CreatedMessage(created, outgoing, post_row)

    @staticmethod
    async def _check_post(
        sender_id: int, channel: Channel, post_id: int | None, new_post: forum.NewPost | None,
    ) -> ForumPost | None:
        """The post a reply belongs to. A forum channel takes only post
        messages and every other channel takes none."""
        if channel.type != "forum":
            if post_id is not None or new_post is not None:
                raise MessageRejected("invalid_post")
            return None
        if new_post is not None:
            if post_id is not None:
                raise MessageRejected("invalid_post")
            return None
        post = post_id is not None and await ForumPost.get_or_none(
            id=post_id, channel_id=channel.id)
        if not post:
            raise MessageRejected("invalid_post")
        if post.locked and not await permissions.has(
                sender_id, channel.server_id, Permission.MANAGE_MESSAGES):
            raise MessageRejected("post_locked")
        return post

    @staticmethod
    async def _reject_stored(sender_id: int, message_id: str, *, channel_id: int) -> None:
        """Raise if *message_id* is already stored: MessageAlreadyStored for the
        same author resending into the same channel (a retry whose ack was
        lost), a duplicate_id rejection for anything else."""
        try:
            existing_uuid = UUID(message_id)
        except ValueError:
            return
        existing = await Message.get_or_none(uuid=existing_uuid)
        if existing is None:
            return
        if existing.author_id == sender_id and existing.channel_id == channel_id:
            raise MessageAlreadyStored
        raise MessageRejected("duplicate_id")

    async def handle_direct_message(
        self, sender_id: int, message: DirectMessageFrame, sender_ws: WebSocket
    ) -> None:
        """Persist a validated DM from *sender_id* and deliver it to the peer.

        Authorization is "sender is a participant of this conversation" — there
        is no server membership to check. As with channel messages, the frame's
        identity is the socket's, not anything the client put in the frame.
        """
        conversation = await Conversation.get_or_none(id=message.conversation_id)
        if conversation is None or not conversation.has_participant(sender_id):
            logger.warning(
                "User %s tried to post to conversation %s without access",
                sender_id, message.conversation_id,
            )
            await self._send_error(sender_ws, "forbidden", message.id)
            return

        if await self._answer_duplicate(
                sender_ws, sender_id, message.id, conversation_id=conversation.id):
            return

        try:
            content = normalize_content(
                message.content, allow_empty=bool(message.attachment_ids))
        except InvalidContent:
            await self._send_error(sender_ws, "invalid_content", message.id)
            return

        attachments = await self._load_attachments(
            sender_id, message.attachment_ids, conversation_id=conversation.id)
        if attachments is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        reply_to_uuid = await self._resolve_reply(
            message.reply_to, conversation_id=conversation.id)
        if reply_to_uuid is _INVALID_REPLY:
            await self._send_error(sender_ws, "invalid_reply", message.id)
            return

        # Persist first: never show the peer a message that was not stored.
        try:
            created = await self._create_message(
                attachments,
                uuid=message.id,
                content=serialize(content),
                author_id=sender_id,
                conversation_id=conversation.id,
                timestamp=message.timestamp,
                metadata=self._stored_metadata(message),
                reply_to_uuid=reply_to_uuid,
            )
        except IntegrityError:
            if not await self._answer_duplicate(
                    sender_ws, sender_id, message.id, conversation_id=conversation.id):
                raise
            return
        if created is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        # Your own message can never leave the thread unread for you, even
        # after a reload on another device.
        await read_state.advance(
            sender_id, conversation_id=conversation.id, message_pk=created.id)

        # Rebuild from known fields so the sender can't smuggle a forged
        # user_id (or anything else) to the peer.
        refs = await reply_refs([reply_to_uuid])
        outgoing = {
            "type": "direct_message",
            "id": message.id,
            "conversation_id": conversation.id,
            "user_id": sender_id,
            "content": content,
            "timestamp": message.timestamp,
            "attachments": [a.to_json() for a in attachments],
            "reactions": [],
            "embeds": [e.model_dump() for e in message.embeds],
            "reply_to": reply_json(reply_to_uuid, refs),
        }

        # Deliver to the peer and back to the sender's other sockets/tabs; only
        # the socket that sent it is excluded, via sender_ws below.
        peer_id = conversation.other_user_id(sender_id)
        await self._fan_out(outgoing, [peer_id, sender_id], sender_ws=sender_ws)
        await self._send_ack(sender_ws, message.id)

        if push.enabled and peer_id != sender_id:
            if self.connection_manager.is_active(peer_id):
                logger.info("Push skipped for user %s: active session", peer_id)
            else:
                push.schedule(self._push_direct_message(
                    sender_id, peer_id, conversation.id, created.id,
                    str(created.uuid), content, attachments))

    async def handle_typing(self, sender_id: int, frame: TypingFrame) -> None:
        """Relay an ephemeral typing indicator to everyone who can see the
        thread except the sender's own sockets. Nothing is stored; frames the
        sender is not allowed to send are dropped without a reply."""
        if frame.conversation_id is not None:
            thread = ("direct", frame.conversation_id)
        else:
            thread = ("channel", frame.channel_id, frame.post_id)
        if not self._accept_typing(sender_id, thread):
            return

        if frame.conversation_id is not None:
            conversation = await Conversation.get_or_none(id=frame.conversation_id)
            if conversation is None or not conversation.has_participant(sender_id):
                logger.debug(
                    "Typing dropped: user %s not in conversation %s",
                    sender_id, frame.conversation_id)
                return
            peer_id = conversation.other_user_id(sender_id)
            outgoing = {
                "type": "typing",
                "user_id": sender_id,
                "conversation_id": conversation.id,
            }
            await self._fan_out(outgoing, [peer_id], exclude_user_id=sender_id)
            return

        can_send = await permissions.has(
            sender_id, frame.server_id, Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES)
        channel = can_send and await Channel.get_or_none(
            id=frame.channel_id, server_id=frame.server_id)
        if not channel:
            logger.debug(
                "Typing dropped: user %s has no access to server %s / channel %s",
                sender_id, frame.server_id, frame.channel_id)
            return
        if channel.type == "forum":
            valid = frame.post_id is not None and await ForumPost.filter(
                id=frame.post_id, channel_id=channel.id).exists()
        else:
            valid = frame.post_id is None
        if not valid:
            logger.debug(
                "Typing dropped: user %s sent post %s for channel %s",
                sender_id, frame.post_id, frame.channel_id)
            return
        outgoing = {
            "type": "typing",
            "user_id": sender_id,
            "server_id": frame.server_id,
            "channel_id": frame.channel_id,
        }
        if frame.post_id is not None:
            outgoing["post_id"] = frame.post_id
        member_ids = await UserToServer.filter(
            server_id=frame.server_id).values_list("user_id", flat=True)
        await self._fan_out(outgoing, member_ids, exclude_user_id=sender_id)

    def _accept_typing(self, sender_id: int, thread: tuple) -> bool:
        """Record and allow a typing frame unless the sender sent one for this
        thread within TYPING_MIN_INTERVAL."""
        now = self.clock()
        key = (sender_id, thread)
        last = self._typing_accepted.get(key)
        if last is not None and now - last < TYPING_MIN_INTERVAL:
            return False
        if len(self._typing_accepted) > _TYPING_PRUNE_THRESHOLD:
            self._typing_accepted = {
                k: t for k, t in self._typing_accepted.items()
                if now - t < TYPING_MIN_INTERVAL
            }
        self._typing_accepted[key] = now
        return True

    async def _push_direct_message(
        self, sender_id: int, peer_id: int, conversation_id: int,
        message_pk: int, message_uuid: str, content, attachments: list[Attachment],
    ) -> None:
        """Web Push for a DM to a peer with no active session."""
        marker = await read_state.get_marker(peer_id, conversation_id=conversation_id)
        unread = await Message.filter(
            conversation_id=conversation_id, author_id=sender_id,
            id__gt=marker or 0).count()
        if unread == 0:
            logger.info("Push skipped for user %s: already read", peer_id)
            return
        sender = await User.get_or_none(id=sender_id)
        if sender is None:
            return
        tag = dm_tag(conversation_id)
        push.track(peer_id, tag, message_pk)
        await push.send_to_user(peer_id, dm_payload(
            conversation_id=conversation_id,
            sender_name=sender.username,
            body=plain_text(content, attachments),
            count=unread,
            message_id=message_pk,
            message_uuid=message_uuid,
        ), topic=tag, urgency="high")

    async def _push_mentions(
        self, sender_id: int, server_id: int, channel_id: int, post_id: int | None,
        message_pk: int, message_uuid: str, content, attachments: list[Attachment],
        mentioned: set[int], member_ids,
    ) -> None:
        """Web Push to server members with no active session who can see the
        channel and were @mentioned."""
        members = set(member_ids)
        recipients = []
        for uid in mentioned:
            if uid == sender_id or uid not in members:
                continue
            if self.connection_manager.is_active(uid):
                logger.info("Push skipped for user %s: active session", uid)
                continue
            recipients.append(uid)
        if not recipients:
            return
        channel = await Channel.get_or_none(id=channel_id)
        sender = await User.get_or_none(id=sender_id)
        if channel is None or sender is None:
            return
        tag = channel_tag(channel_id)
        payload = mention_payload(
            server_id=server_id,
            channel_id=channel_id,
            channel_name=channel.name,
            post_id=post_id,
            sender_name=sender.username,
            body=plain_text(content, attachments),
            message_id=message_pk,
            message_uuid=message_uuid,
        )
        for uid in recipients:
            if not await permissions.has(uid, server_id, Permission.VIEW_CHANNEL):
                logger.info("Push skipped for user %s: cannot view channel", uid)
                continue
            push.track(uid, tag, message_pk)
            await push.send_to_user(uid, payload, topic=tag, urgency="high")

    @staticmethod
    async def _resolve_reply(
        raw: str | None,
        *,
        channel_id: int | None = None,
        conversation_id: int | None = None,
        post_id: int | None = None,
    ) -> UUID | None | object:
        """The original's uuid, None for a non-reply, or _INVALID_REPLY if it
        isn't a uuid or isn't a message of this channel or conversation (and,
        in a forum, of the same post)."""
        if raw is None:
            return None
        try:
            original = UUID(raw)
        except ValueError:
            return _INVALID_REPLY
        exists = await Message.filter(
            uuid=original, channel_id=channel_id, conversation_id=conversation_id,
            post_id=post_id,
        ).exists()
        return original if exists else _INVALID_REPLY

    @staticmethod
    async def _load_attachments(
        sender_id: int,
        attachment_ids: list[str],
        *,
        channel_id: int | None = None,
        conversation_id: int | None = None,
    ) -> list[Attachment] | None:
        """Resolve the frame's attachment ids, in order, or None if any of them
        is unknown, someone else's, already sent, or uploaded for another
        channel or conversation."""
        if not attachment_ids:
            return []
        try:
            ids = [UUID(raw) for raw in attachment_ids]
        except ValueError:
            return None
        if len(set(ids)) != len(ids):
            return None

        found = {a.id: a for a in await Attachment.filter(id__in=ids)}
        attachments = []
        for attachment_id in ids:
            attachment = found.get(attachment_id)
            if (
                attachment is None
                or attachment.uploader_id != sender_id
                or attachment.message_id is not None
                or attachment.channel_id != channel_id
                or attachment.conversation_id != conversation_id
            ):
                return None
            attachments.append(attachment)
        return attachments

    @staticmethod
    def _stored_metadata(message: MessageFrame | DirectMessageFrame) -> dict:
        """Client metadata with embeds replaced by the validated ones."""
        metadata = {k: v for k, v in message.metadata.items() if k != "embeds"}
        if message.embeds:
            metadata["embeds"] = [e.model_dump() for e in message.embeds]
        return metadata

    @staticmethod
    async def _create_message(
        attachments: list[Attachment],
        *,
        new_post: forum.NewPost | None = None,
        reply_post: ForumPost | None = None,
        **fields,
    ) -> Message | None:
        """Store the message, link its attachments and keep its post in step,
        in one transaction. Returns None, storing nothing, if an attachment
        got taken meanwhile."""
        try:
            async with in_transaction():
                post = reply_post
                if new_post is not None:
                    post = await forum.create_post(
                        fields["channel_id"], fields["author_id"], new_post)
                created = await Message.create(
                    post_id=post.id if post else None, **fields)
                if attachments:
                    linked = await Attachment.filter(
                        id__in=[a.id for a in attachments], message_id__isnull=True,
                    ).update(message_id=created.id)
                    if linked != len(attachments):
                        raise _AttachmentsUnavailable
                if new_post is not None:
                    post.opening_message_id = created.id
                    await post.save(update_fields=["opening_message_id"])
                elif post is not None:
                    await ForumPost.filter(id=post.id).update(
                        reply_count=F("reply_count") + 1, last_activity_at=forum.utcnow())
        except _AttachmentsUnavailable:
            return None
        return created

    async def _fan_out(
        self,
        outgoing: dict,
        recipient_ids,
        *,
        sender_ws: WebSocket | None = None,
        exclude_user_id: int | None = None,
    ) -> None:
        """Send *outgoing* to every socket of each recipient, skipping only the
        sending socket itself (not the whole sender) so the author's other
        tabs still receive their own message. *exclude_user_id* skips every
        socket of that user instead.

        One dead recipient must not take down the sender's socket or stop
        delivery to everyone after them.
        """
        for uid in recipient_ids:
            if uid == exclude_user_id:
                continue
            for websocket in self.connection_manager.get_websockets(uid):
                if websocket is sender_ws:
                    continue
                try:
                    await websocket.send_json(outgoing)
                except Exception:
                    logger.warning(
                        "Failed to deliver message to user %s", uid, exc_info=True)

    async def _answer_duplicate(
        self,
        sender_ws: WebSocket,
        sender_id: int,
        message_id: str,
        *,
        channel_id: int | None = None,
        conversation_id: int | None = None,
    ) -> bool:
        """If *message_id* is already stored, answer the sender and return True.

        The same author resending into the same thread is a retry whose ack was
        lost, so it is acked again; anything else is an id clash.
        """
        try:
            existing_uuid = UUID(message_id)
        except ValueError:
            return False
        existing = await Message.get_or_none(uuid=existing_uuid)
        if existing is None:
            return False
        if (
            existing.author_id == sender_id
            and existing.channel_id == channel_id
            and existing.conversation_id == conversation_id
        ):
            await self._send_ack(sender_ws, message_id)
        else:
            await self._send_error(sender_ws, "duplicate_id", message_id)
        return True

    @staticmethod
    async def _send_ack(websocket: WebSocket, message_id: str) -> None:
        try:
            await websocket.send_json({"type": "message_ack", "id": message_id})
        except Exception:
            logger.warning("Failed to send message ack", exc_info=True)

    @staticmethod
    async def _send_error(websocket: WebSocket, code: str, ref: str | None) -> None:
        try:
            await websocket.send_json({"type": "error", "code": code, "ref": ref})
        except Exception:
            logger.warning("Failed to send error frame", exc_info=True)
