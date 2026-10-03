import time

from fastapi import WebSocket

IDLE_AFTER_SECONDS = 300


class ConnectionManager:
    """Maintains bidirectional mapping between user IDs and WebSocket connections.

    A user may have more than one live socket (multiple tabs/devices), so each
    user id maps to a *set* of sockets rather than a single one.

    A socket is *active* while its client reported activity within the last
    ``IDLE_AFTER_SECONDS``; sockets start idle. Like the socket maps, this is
    per-process.
    """

    clock = staticmethod(time.monotonic)

    def __init__(self) -> None:
        self.user_to_websockets: dict[int, set[WebSocket]] = {}
        self.websocket_to_user: dict[WebSocket, int] = {}
        self.websocket_to_token: dict[WebSocket, str] = {}
        self.last_active: dict[WebSocket, float] = {}

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
        self.last_active.pop(websocket, None)
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

    def online_user_ids(self) -> list[int]:
        return list(self.user_to_websockets)

    def online_and_active_counts(self) -> tuple[int, int]:
        online = self.online_user_ids()
        return len(online), sum(1 for user_id in online if self.is_active(user_id))

    def set_activity(self, websocket: WebSocket, active: bool) -> None:
        if websocket not in self.websocket_to_user:
            return
        if active:
            self.last_active[websocket] = self.clock()
        else:
            self.last_active.pop(websocket, None)

    def is_active(self, user_id: int) -> bool:
        now = self.clock()
        return any(
            now - self.last_active[ws] < IDLE_AFTER_SECONDS
            for ws in self.user_to_websockets.get(user_id, ())
            if ws in self.last_active
        )
