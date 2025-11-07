from datetime import datetime
from fastapi import WebSocket

from app.models.UserToServer import UserToServer


class Communication:
    def __init__(self):
        self.user_to_websocket = {}
        self.websocket_to_user = {}

    def add_connection(self, user_id, websocket):
        self.user_to_websocket[user_id] = websocket
        self.websocket_to_user[websocket] = user_id

    def remove_connection_by_user_id(self, user_id):
        if user_id in self.user_to_websocket:
            websocket = self.user_to_websocket[user_id]
            del self.user_to_websocket[user_id]
            del self.websocket_to_user[websocket]

    def remove_connection_by_websocket(self, websocket: WebSocket):
        if websocket in self.websocket_to_user:
            user_id = self.websocket_to_user[websocket]
            del self.websocket_to_user[websocket]
            del self.user_to_websocket[user_id]

    async def forward_message(self, message: dict, server_id: str, channel_id: str):
        # get users in the server
        users_in_server = await UserToServer.filter(
            server_id=server_id).values_list('user_id', flat=True)

        for user_id in users_in_server:
            if user_id in self.user_to_websocket and user_id != message.get("user_id"):
                websocket = self.user_to_websocket[user_id]
                await websocket.send_json(message)

    async def message_switch(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        server_id = message.get("server_id")
        channel_id = message.get("channel_id")

        message_type = message.get("type")

        if message_type == "connection_init":
            self.add_connection(user_id, websocket)
        elif message_type == "disconnect":
            self.remove_connection(user_id)
        elif message_type == "message":
            await self.forward_message(message, server_id, channel_id)
