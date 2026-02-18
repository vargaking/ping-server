import logging

from fastapi import WebSocket

from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager
from app.services.voice_manager import VoiceManager
from app.services.chat_service import ChatService

logger = logging.getLogger("app.communication")


class Communication:
    """Orchestrates WebSocket message routing across chat and voice services."""

    def __init__(self) -> None:
        self.connection_manager = ConnectionManager()
        self.voice_manager = VoiceManager(self.connection_manager)
        self.chat_service = ChatService(self.connection_manager)

    def add_connection(self, user_id: int, websocket: WebSocket) -> None:
        self.connection_manager.add_connection(user_id, websocket)

    async def remove_connection_by_user_id(self, user_id: int) -> None:
        self.connection_manager.remove_connection_by_user_id(user_id)
        await self.voice_manager.cleanup_user(user_id)
        await self._notify_presence(user_id, online=False)

    async def remove_connection_by_websocket(self, websocket: WebSocket):
        user_id = self.connection_manager.remove_connection_by_websocket(
            websocket)
        if user_id:
            await self.voice_manager.cleanup_user(user_id)
            await self._notify_presence(user_id, online=False)

    async def message_switch(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")

        message_type = message.get("type")

        if message_type == "connection_init":
            self.add_connection(user_id, websocket)
            await self._notify_presence(user_id, online=True)
        elif message_type == "disconnect":
            await self.remove_connection_by_user_id(user_id)
        elif message_type == "message":
            await self.chat_service.handle_message(message)
        elif message_type == "join_voice":
            await self.voice_manager.handle_voice_join(message, websocket)
        elif message_type == "leave_voice":
            await self.voice_manager.handle_voice_leave(message, websocket)
        elif message_type == "producer_created":
            await self.voice_manager.handle_producer_created(message, websocket)
        elif message_type == "voice_signal":
            await self.voice_manager.handle_voice_signal(message, websocket)

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
            # Determine which related users are currently online
            online_related_ids = [
                uid for uid in related_user_ids
                if self.connection_manager.is_online(uid)
            ]

            # Send the newly connected user the current online list
            ws = self.connection_manager.get_websocket(user_id)
            if ws:
                try:
                    await ws.send_json({
                        "type": "presence_init",
                        "user_ids": online_related_ids,
                    })
                except Exception:
                    logger.warning(
                        "Failed to send presence_init to user %s",
                        user_id,
                        exc_info=True,
                    )

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
