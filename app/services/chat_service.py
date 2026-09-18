import json
import logging

from fastapi import WebSocket

from app.models.Channel import Channel
from app.models.Message import Message
from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager

logger = logging.getLogger("app.services.chat_service")


class ChatService:
    """Handles incoming chat messages: persists to DB and broadcasts to server members."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self.connection_manager = connection_manager

    async def handle_message(
        self, sender_id: int, message: dict, sender_ws: WebSocket
    ) -> None:
        """Persist a chat message from *sender_id* and fan it out.

        *sender_id* is the authenticated owner of the socket. Whatever
        ``user_id`` the client put in the frame is discarded.
        """
        server_id = message.get("server_id")
        channel_id = message.get("channel_id")

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
            await self._send_error(sender_ws, "forbidden", message.get("id"))
            return

        content_payload = message.get("content")
        if isinstance(content_payload, dict):
            content_payload = json.dumps(content_payload)

        # Persist first: never show other people a message that was not stored.
        await Message.create(
            uuid=message.get("id"),
            content=content_payload,
            author_id=sender_id,
            server_id=server_id,
            channel_id=channel_id,
            timestamp=message.get("timestamp"),
            metadata=message.get("metadata", {}),
        )

        # Rebuild the frame from known fields instead of relaying the client's
        # dict, so the sender can't smuggle a forged user_id (or anything else)
        # to other clients.
        outgoing = {
            "type": "message",
            "id": message.get("id"),
            "server_id": server_id,
            "channel_id": channel_id,
            "user_id": sender_id,
            "content": message.get("content"),
            "timestamp": message.get("timestamp"),
        }

        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)

        for uid in member_ids:
            if uid == sender_id:
                continue
            websocket = self.connection_manager.get_websocket(uid)
            if websocket is None:
                continue
            try:
                await websocket.send_json(outgoing)
            except Exception:
                # One dead recipient must not take down the sender's socket or
                # stop delivery to everyone after them.
                logger.warning(
                    "Failed to deliver message to user %s", uid, exc_info=True)

    @staticmethod
    async def _send_error(websocket: WebSocket, code: str, ref: str | None) -> None:
        try:
            await websocket.send_json({"type": "error", "code": code, "ref": ref})
        except Exception:
            logger.warning("Failed to send error frame", exc_info=True)
