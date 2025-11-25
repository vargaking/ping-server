from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.user_to_websocket = {}
        self.websocket_to_user = {}

    def add_connection(self, user_id: int, websocket: WebSocket):
        self.user_to_websocket[user_id] = websocket
        self.websocket_to_user[websocket] = user_id

    def remove_connection_by_user_id(self, user_id: int):
        if user_id in self.user_to_websocket:
            websocket = self.user_to_websocket[user_id]
            del self.user_to_websocket[user_id]
            if websocket in self.websocket_to_user:
                del self.websocket_to_user[websocket]
            return websocket
        return None

    def remove_connection_by_websocket(self, websocket: WebSocket):
        if websocket in self.websocket_to_user:
            user_id = self.websocket_to_user[websocket]
            del self.websocket_to_user[websocket]
            if user_id in self.user_to_websocket:
                del self.user_to_websocket[user_id]
            return user_id
        return None

    def get_websocket(self, user_id: int) -> WebSocket | None:
        return self.user_to_websocket.get(user_id)

    def get_user_id(self, websocket: WebSocket) -> int | None:
        return self.websocket_to_user.get(websocket)
