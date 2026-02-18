from fastapi import WebSocket


class ConnectionManager:
    """Maintains bidirectional mapping between user IDs and WebSocket connections."""

    def __init__(self) -> None:
        self.user_to_websocket: dict[int, WebSocket] = {}
        self.websocket_to_user: dict[WebSocket, int] = {}

    def add_connection(self, user_id: int, websocket: WebSocket) -> None:
        self.user_to_websocket[user_id] = websocket
        self.websocket_to_user[websocket] = user_id

    def remove_connection_by_user_id(self, user_id: int) -> WebSocket | None:
        websocket = self.user_to_websocket.pop(user_id, None)
        if websocket is not None:
            self.websocket_to_user.pop(websocket, None)
        return websocket

    def remove_connection_by_websocket(self, websocket: WebSocket) -> int | None:
        user_id = self.websocket_to_user.pop(websocket, None)
        if user_id is not None:
            self.user_to_websocket.pop(user_id, None)
        return user_id

    def get_websocket(self, user_id: int) -> WebSocket | None:
        return self.user_to_websocket.get(user_id)

    def get_user_id(self, websocket: WebSocket) -> int | None:
        return self.websocket_to_user.get(websocket)

    def is_online(self, user_id: int) -> bool:
        return user_id in self.user_to_websocket
