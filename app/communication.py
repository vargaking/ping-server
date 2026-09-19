import logging

from fastapi import WebSocket

from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager
from app.services.chat_service import ChatService
from app.ws_schemas import (
    ConnectionInitFrame,
    DisconnectFrame,
    MessageFrame,
    ValidationError,
    parse_frame,
)

logger = logging.getLogger("app.communication")


class Communication:
    """Orchestrates WebSocket message routing across chat and voice services."""

    def __init__(self) -> None:
        self.connection_manager = ConnectionManager()
        self.chat_service = ChatService(self.connection_manager)

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        """Register an *authenticated* socket and announce the user online.

        ``user_id`` must come from the session resolved during the handshake,
        never from anything the client sent.
        """
        self.connection_manager.add_connection(user_id, websocket)
        await self._notify_presence(user_id, online=True)

    async def remove_connection_by_websocket(self, websocket: WebSocket):
        user_id = self.connection_manager.remove_connection_by_websocket(
            websocket)
        # Only announce offline if the user has no remaining live socket: a
        # refresh may have already reconnected them on a new socket, in which
        # case the old socket's close must not flap their presence.
        if user_id and not self.connection_manager.is_online(user_id):
            await self._notify_presence(user_id, online=False)

    async def message_switch(self, message, websocket: WebSocket):
        # Identity is whatever the handshake established for this socket. Any
        # ``user_id`` inside the frame is ignored.
        user_id = self.connection_manager.get_user_id(websocket)
        if user_id is None:
            return

        # Validate the frame before doing anything with it. A malformed frame
        # gets a single error frame back and the socket stays open — a bad
        # message must never crash the connection loop.
        try:
            frame = parse_frame(message)
        except ValidationError:
            ref = message.get("id") if isinstance(message, dict) else None
            await self._send_error(websocket, "invalid_frame", ref)
            return

        if isinstance(frame, ConnectionInitFrame):
            # Legacy frame: registration now happens at handshake. Older
            # frontends still send it, so answer with a fresh presence snapshot
            # instead of erroring.
            await self._send_presence_snapshot(user_id, websocket)
        elif isinstance(frame, DisconnectFrame):
            await self.remove_connection_by_websocket(websocket)
        elif isinstance(frame, MessageFrame):
            await self.chat_service.handle_message(user_id, frame, websocket)

    @staticmethod
    async def _send_error(websocket: WebSocket, code: str, ref: str | None) -> None:
        try:
            await websocket.send_json({"type": "error", "code": code, "ref": ref})
        except Exception:
            logger.warning("Failed to send error frame", exc_info=True)

    @staticmethod
    async def get_related_user_ids(user_id: int) -> set[int]:
        """Return the set of user IDs who share at least one server with *user_id*,
        excluding *user_id* itself.

        Useful for broadcasting invalidation events, online-presence lists, etc.
        """
        user_server_ids = await UserToServer.filter(
            user_id=user_id
        ).values_list("server_id", flat=True)

        return set(
            await UserToServer.filter(
                server_id__in=user_server_ids
            ).values_list("user_id", flat=True)
        ) - {user_id}

    async def _send_presence_snapshot(
        self,
        user_id: int,
        websocket: WebSocket,
        *,
        related_user_ids: set[int] | None = None,
    ) -> None:
        """Send *websocket* the list of related users who are currently online."""
        if related_user_ids is None:
            related_user_ids = await self.get_related_user_ids(user_id)
        try:
            await websocket.send_json({
                "type": "presence_init",
                "user_ids": [
                    uid for uid in related_user_ids
                    if self.connection_manager.is_online(uid)
                ],
            })
        except Exception:
            logger.warning(
                "Failed to send presence_init to user %s", user_id, exc_info=True)

    async def _notify_presence(self, user_id: int, *, online: bool) -> None:
        """Handle presence notifications on connect/disconnect.

        When *online* is True (user just connected):
          1. Send the connecting user the list of their related users who are
             currently online.
          2. Notify those related online users that this user came online.

        When *online* is False (user disconnected):
          Notify related online users that this user went offline.
        """
        related_user_ids = await self.get_related_user_ids(user_id)

        if online:
            ws = self.connection_manager.get_websocket(user_id)
            if ws:
                await self._send_presence_snapshot(
                    user_id, ws, related_user_ids=related_user_ids)

        # Broadcast the status change to related online users
        status_message = {
            "type": "presence_update",
            "user_id": user_id,
            "online": online,
        }

        for uid in related_user_ids:
            ws = self.connection_manager.get_websocket(uid)
            if ws:
                try:
                    await ws.send_json(status_message)
                except Exception:
                    logger.warning(
                        "Failed to send presence_update to user %s",
                        uid,
                        exc_info=True,
                    )

    async def notify_user_invalidate(self, user_id: int):
        """Notify related users that a user's profile has changed.

        Sends a lightweight invalidation message (just the user_id) to users
        who share a server with the updated user. Clients should fetch the
        updated profile via REST if they need the new data.
        """
        related_user_ids = await self.get_related_user_ids(user_id)

        message = {
            "type": "user_invalidate",
            "user_id": user_id,
        }

        for uid in related_user_ids:
            ws = self.connection_manager.get_websocket(uid)
            if ws:
                try:
                    await ws.send_json(message)
                except Exception:
                    logger.warning(
                        "Failed to send user invalidate to user %s",
                        uid,
                        exc_info=True,
                    )
