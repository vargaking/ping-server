import logging

from app.models.Message import Message
from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager

logger = logging.getLogger("app.services.chat_service")


class ChatService:
    """Handles incoming chat messages: broadcasts to server members and persists to DB."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self.connection_manager = connection_manager

    async def handle_message(self, message: dict) -> None:
        """Broadcast *message* to other server members and save it to the database."""
        server_id = message.get("server_id")
        channel_id = message.get("channel_id")
        user_id = message.get("user_id")

        # get users in the server
        users_in_server = await UserToServer.filter(
            server_id=server_id).values_list('user_id', flat=True)

        for uid in users_in_server:
            if uid != user_id:
                websocket = self.connection_manager.get_websocket(uid)
                if websocket:
                    await websocket.send_json(message)

        # Save message to database
        await Message.create(
            uuid=message.get("id"),
            content=message.get("content"),
            author_id=user_id,
            server_id=server_id,
            channel_id=channel_id,
            timestamp=message.get("timestamp"),
            metadata=message.get("metadata", {})
        )
