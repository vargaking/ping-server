import json
import logging
from uuid import UUID

from fastapi import WebSocket
from tortoise.transactions import in_transaction

from app.models.Attachment import Attachment
from app.models.Channel import Channel
from app.models.Conversation import Conversation
from app.models.Message import Message
from app.models.UserToServer import UserToServer
from app.services import read_state
from app.services.connection_manager import ConnectionManager
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

        # Being logged in is not enough: the sender has to be a member of the
        # server, and the channel has to actually live in that server.
        is_member = await UserToServer.filter(
            user_id=sender_id, server_id=server_id).exists()
        channel_ok = is_member and await Channel.filter(
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
        }

        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)

        await self._fan_out(outgoing, member_ids, sender_ws=sender_ws)

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
        }

        # Deliver to the peer and back to the sender's other sockets/tabs; only
        # the socket that sent it is excluded, via sender_ws below.
        peer_id = conversation.other_user_id(sender_id)
        await self._fan_out(outgoing, [peer_id, sender_id], sender_ws=sender_ws)

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
