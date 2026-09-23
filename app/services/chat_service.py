import json
import logging

from fastapi import WebSocket

from app.models.Channel import Channel
from app.models.Conversation import Conversation
from app.models.Message import Message
from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager
from app.ws_schemas import DirectMessageFrame, MessageFrame

logger = logging.getLogger("app.services.chat_service")


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

        content_payload = message.content
        if isinstance(content_payload, dict):
            content_payload = json.dumps(content_payload)

        # Persist first: never show other people a message that was not stored.
        await Message.create(
            uuid=message.id,
            content=content_payload,
            author_id=sender_id,
            server_id=server_id,
            channel_id=channel_id,
            timestamp=message.timestamp,
            metadata=message.metadata,
        )

        # Rebuild the frame from known fields instead of relaying the client's
        # dict, so the sender can't smuggle a forged user_id (or anything else)
        # to other clients.
        outgoing = {
            "type": "message",
            "id": message.id,
            "server_id": server_id,
            "channel_id": channel_id,
            "user_id": sender_id,
            "content": message.content,
            "timestamp": message.timestamp,
        }

        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)

        await self._fan_out(outgoing, member_ids, exclude_id=sender_id)

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

        content_payload = message.content
        if isinstance(content_payload, dict):
            content_payload = json.dumps(content_payload)

        # Persist first: never show the peer a message that was not stored.
        await Message.create(
            uuid=message.id,
            content=content_payload,
            author_id=sender_id,
            conversation_id=conversation.id,
            timestamp=message.timestamp,
            metadata=message.metadata,
        )

        # Rebuild from known fields so the sender can't smuggle a forged
        # user_id (or anything else) to the peer.
        outgoing = {
            "type": "direct_message",
            "id": message.id,
            "conversation_id": conversation.id,
            "user_id": sender_id,
            "content": message.content,
            "timestamp": message.timestamp,
        }

        # Deliver to the other participant. The sender already has it locally,
        # matching the channel path's exclude-sender behaviour.
        peer_id = conversation.other_user_id(sender_id)
        await self._fan_out(outgoing, [peer_id], exclude_id=sender_id)

    async def _fan_out(
        self, outgoing: dict, recipient_ids, *, exclude_id: int | None = None
    ) -> None:
        """Send *outgoing* to each connected recipient, skipping *exclude_id*.

        One dead recipient must not take down the sender's socket or stop
        delivery to everyone after them.
        """
        for uid in recipient_ids:
            if uid == exclude_id:
                continue
            websocket = self.connection_manager.get_websocket(uid)
            if websocket is None:
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
