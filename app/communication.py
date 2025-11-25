from fastapi import WebSocket

from app.services.connection_manager import ConnectionManager
from app.services.voice_manager import VoiceManager
from app.services.chat_service import ChatService

class Communication:
    def __init__(self):
        self.connection_manager = ConnectionManager()
        self.voice_manager = VoiceManager(self.connection_manager)
        self.chat_service = ChatService(self.connection_manager)

    def add_connection(self, user_id, websocket):
        self.connection_manager.add_connection(user_id, websocket)

    async def remove_connection_by_user_id(self, user_id):
        self.connection_manager.remove_connection_by_user_id(user_id)
        await self.voice_manager.cleanup_user(user_id)

    async def remove_connection_by_websocket(self, websocket: WebSocket):
        user_id = self.connection_manager.remove_connection_by_websocket(websocket)
        if user_id:
            await self.voice_manager.cleanup_user(user_id)

    async def message_switch(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        
        message_type = message.get("type")

        if message_type == "connection_init":
            self.add_connection(user_id, websocket)
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
