from fastapi import WebSocket


class ConnectionManager:
    """Maintains bidirectional mapping between user IDs and WebSocket connections.

    A user may have more than one live socket (multiple tabs/devices), so each
    user id maps to a *set* of sockets rather than a single one.
    """

    def __init__(self) -> None:
        self.user_to_websockets: dict[int, set[WebSocket]] = {}
        self.websocket_to_user: dict[WebSocket, int] = {}
        self.websocket_to_token: dict[WebSocket, str] = {}

    def add_connection(
        self, user_id: int, websocket: WebSocket, token: str | None = None
    ) -> None:
        self.user_to_websockets.setdefault(user_id, set()).add(websocket)
        self.websocket_to_user[websocket] = user_id
        if token is not None:
            self.websocket_to_token[websocket] = token

    def remove_connection_by_websocket(self, websocket: WebSocket) -> int | None:
        user_id = self.websocket_to_user.pop(websocket, None)
        self.websocket_to_token.pop(websocket, None)
        if user_id is not None:
            sockets = self.user_to_websockets.get(user_id)
            if sockets is not None:
                sockets.discard(websocket)
                if not sockets:
                    del self.user_to_websockets[user_id]
        return user_id

    def get_websockets(self, user_id: int) -> list[WebSocket]:
        return list(self.user_to_websockets.get(user_id, ()))

    def get_websockets_for_token(self, token: str) -> list[WebSocket]:
        return [ws for ws, t in self.websocket_to_token.items() if t == token]

    def get_user_id(self, websocket: WebSocket) -> int | None:
        return self.websocket_to_user.get(websocket)

    def is_online(self, user_id: int) -> bool:
        return bool(self.user_to_websockets.get(user_id))
