import json
import logging
from uuid import UUID

from fastapi import WebSocket
from tortoise.transactions import in_transaction

from app.models.Attachment import Attachment
from app.models.Channel import Channel
from app.models.Conversation import Conversation
from app.models.Message import Message
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.permissions import Permission
from app.services import read_state
from app.services.connection_manager import ConnectionManager
from app.services.permissions import permissions
from app.services.push import (
    channel_tag, dm_payload, dm_tag, mention_payload, mentioned_user_ids, plain_text, push)
from app.ws_schemas import DirectMessageFrame, MessageFrame

logger = logging.getLogger("app.services.chat_service")


class _AttachmentsUnavailable(Exception):
    """An attachment was linked to another message between the check and the
    write; the message must not be stored."""


class ChatService:
    """Handles incoming chat messages: persists to DB and broadcasts to server members."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self.connection_manager = connection_manager

    async def handle_message(
        self, sender_id: int, message: MessageFrame, sender_ws: WebSocket
    ) -> None:
        """Persist a validated chat message from *sender_id* and fan it out.

        *sender_id* is the authenticated owner of the socket. The frame has
        already been validated (see ws_schemas); any ``user_id`` the client put
        in it is discarded.
        """
        server_id = message.server_id
        channel_id = message.channel_id

        # Being logged in is not enough: the sender has to be allowed to post
        # in the server, and the channel has to actually live in that server.
        can_send = await permissions.has(
            sender_id, server_id, Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES)
        channel_ok = can_send and await Channel.filter(
            id=channel_id, server_id=server_id).exists()
        if not channel_ok:
            logger.warning(
                "User %s tried to post to server %s / channel %s without access",
                sender_id, server_id, channel_id,
            )
            await self._send_error(sender_ws, "forbidden", message.id)
            return

        attachments = await self._load_attachments(
            sender_id, message.attachment_ids, channel_id=channel_id)
        if attachments is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        content = message.content
        if attachments and content is None:
            content = ""
        content_payload = content
        if isinstance(content_payload, dict):
            content_payload = json.dumps(content_payload)

        # Persist first: never show other people a message that was not stored.
        created = await self._create_message(
            attachments,
            uuid=message.id,
            content=content_payload,
            author_id=sender_id,
            server_id=server_id,
            channel_id=channel_id,
            timestamp=message.timestamp,
            metadata=message.metadata,
        )
        if created is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        # Your own message can never leave the thread unread for you, even
        # after a reload on another device.
        await read_state.advance(
            sender_id, channel_id=channel_id, message_pk=created.id)

        # Rebuild the frame from known fields instead of relaying the client's
        # dict, so the sender can't smuggle a forged user_id (or anything else)
        # to other clients.
        outgoing = {
            "type": "message",
            "id": message.id,
            "server_id": server_id,
            "channel_id": channel_id,
            "user_id": sender_id,
            "content": content,
            "timestamp": message.timestamp,
            "attachments": [a.to_json() for a in attachments],
            "reactions": [],
        }

        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)

        await self._fan_out(outgoing, member_ids, sender_ws=sender_ws)

        if push.enabled:
            # Runs in the sender's socket path: a push problem must never
            # break message delivery.
            try:
                mentioned = mentioned_user_ids(content)
                if mentioned:
                    push.schedule(self._push_mentions(
                        sender_id, server_id, channel_id, created.id,
                        str(created.uuid), content, attachments, mentioned, member_ids))
            except Exception:
                logger.warning("Failed to schedule mention pushes", exc_info=True)

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

        attachments = await self._load_attachments(
            sender_id, message.attachment_ids, conversation_id=conversation.id)
        if attachments is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        content = message.content
        if attachments and content is None:
            content = ""
        content_payload = content
        if isinstance(content_payload, dict):
            content_payload = json.dumps(content_payload)

        # Persist first: never show the peer a message that was not stored.
        created = await self._create_message(
            attachments,
            uuid=message.id,
            content=content_payload,
            author_id=sender_id,
            conversation_id=conversation.id,
            timestamp=message.timestamp,
            metadata=message.metadata,
        )
        if created is None:
            await self._send_error(sender_ws, "invalid_attachments", message.id)
            return

        # Your own message can never leave the thread unread for you, even
        # after a reload on another device.
        await read_state.advance(
            sender_id, conversation_id=conversation.id, message_pk=created.id)

        # Rebuild from known fields so the sender can't smuggle a forged
        # user_id (or anything else) to the peer.
        outgoing = {
            "type": "direct_message",
            "id": message.id,
            "conversation_id": conversation.id,
            "user_id": sender_id,
            "content": content,
            "timestamp": message.timestamp,
            "attachments": [a.to_json() for a in attachments],
            "reactions": [],
        }

        # Deliver to the peer and back to the sender's other sockets/tabs; only
        # the socket that sent it is excluded, via sender_ws below.
        peer_id = conversation.other_user_id(sender_id)
        await self._fan_out(outgoing, [peer_id, sender_id], sender_ws=sender_ws)

        if push.enabled and peer_id != sender_id:
            if self.connection_manager.is_active(peer_id):
                logger.info("Push skipped for user %s: active session", peer_id)
            else:
                push.schedule(self._push_direct_message(
                    sender_id, peer_id, conversation.id, created.id,
                    str(created.uuid), content, attachments))

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
        self, sender_id: int, server_id: int, channel_id: int, message_pk: int,
        message_uuid: str, content, attachments: list[Attachment],
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
    async def _create_message(attachments: list[Attachment], **fields) -> Message | None:
        """Store the message and link its attachments in one transaction.
        Returns None, storing nothing, if an attachment got taken meanwhile."""
        try:
            async with in_transaction():
                created = await Message.create(**fields)
                if attachments:
                    linked = await Attachment.filter(
                        id__in=[a.id for a in attachments], message_id__isnull=True,
                    ).update(message_id=created.id)
                    if linked != len(attachments):
                        raise _AttachmentsUnavailable
        except _AttachmentsUnavailable:
            return None
        return created

    async def _fan_out(
        self, outgoing: dict, recipient_ids, *, sender_ws: WebSocket | None = None
    ) -> None:
        """Send *outgoing* to every socket of each recipient, skipping only the
        sending socket itself (not the whole sender) so the author's other
        tabs still receive their own message.

        One dead recipient must not take down the sender's socket or stop
        delivery to everyone after them.
        """
        for uid in recipient_ids:
            for websocket in self.connection_manager.get_websockets(uid):
                if websocket is sender_ws:
                    continue
                try:
                    await websocket.send_json(outgoing)
                except Exception:
                    logger.warning(
                        "Failed to deliver message to user %s", uid, exc_info=True)

    @staticmethod
    async def _send_error(websocket: WebSocket, code: str, ref: str | None) -> None:
        try:
            await websocket.send_json({"type": "error", "code": code, "ref": ref})
        except Exception:
            logger.warning("Failed to send error frame", exc_info=True)
