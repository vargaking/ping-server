import asyncio
import logging

from fastapi import WebSocket

from tortoise.expressions import Q

from app.models.Conversation import Conversation
from app.models.Server import Server
from app.models.UserToServer import UserToServer
from app.services.activity import touch_last_active
from app.services.permissions import permissions
from app.services.connection_manager import ConnectionManager
from app.services.socket_send import close_quietly, send_frame, send_frame_to_all
from app.services.chat_service import ChatService
from app.services.friends import friend_ids, pending_counterpart_ids
from app.ws_schemas import (
    ActivityFrame,
    PingFrame,
    ConnectionInitFrame,
    DirectMessageFrame,
    DisconnectFrame,
    MessageFrame,
    TypingFrame,
    ValidationError,
    parse_frame,
)

logger = logging.getLogger("app.communication")

# Application-level close code: "no valid session". The frontend must not
# auto-reconnect on this one (it would loop forever); it should go to login.
WS_CLOSE_UNAUTHENTICATED = 4401

# Sent to a socket that stopped taking frames. Anything but 4401 makes the
# frontend reconnect.
WS_CLOSE_STALLED = 1013


class Communication:
    """Orchestrates WebSocket message routing across chat and voice services."""

    def __init__(self) -> None:
        self._background: set[asyncio.Task] = set()
        self.connection_manager = ConnectionManager()
        self.chat_service = ChatService(self.connection_manager, self._drop_stalled)

    async def connect(
        self, user_id: int, websocket: WebSocket, token: str | None = None
    ) -> None:
        """Register an *authenticated* socket and announce the user online.

        ``user_id`` must come from the session resolved during the handshake,
        never from anything the client sent. A user may already have other
        live sockets (another tab); the connecting socket always gets its own
        presence_init snapshot, but the online presence_update only fires the
        first time the user goes from 0 to 1 sockets.
        """
        await touch_last_active(user_id)
        was_online = self.connection_manager.is_online(user_id)
        self.connection_manager.add_connection(user_id, websocket, token)
        await self._notify_presence(user_id, online=True, websocket=websocket,
                                     was_online=was_online)
        await self._send_permissions_snapshot(user_id, websocket)

    async def remove_connection_by_websocket(self, websocket: WebSocket):
        user_id = self._forget(websocket)
        if user_id is not None:
            await self._notify_presence(user_id, online=False)

    def _forget(self, websocket: WebSocket) -> int | None:
        """Unregister *websocket*. Returns the user's id if that was their last
        socket, so they must now be announced offline.

        A refresh may have already reconnected them on a new socket, or they
        may simply have another tab open, and either case must not flap their
        presence.
        """
        user_id = self.connection_manager.remove_connection_by_websocket(websocket)
        if user_id is not None and not self.connection_manager.is_online(user_id):
            return user_id
        return None

    def _drop_stalled(self, websocket: WebSocket) -> None:
        """Forget a socket that stopped taking frames, then close it and
        announce its user offline without making the sender wait. Registered
        before anything is awaited, so no other broadcast waits on it again."""
        if self.connection_manager.get_user_id(websocket) is None:
            return
        user_id = self._forget(websocket)
        self._spawn(close_quietly(websocket, WS_CLOSE_STALLED))
        if user_id is not None:
            self._spawn(self._notify_presence(user_id, online=False))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Socket cleanup failed", exc_info=task.exception())

    async def close_session(self, token: str) -> None:
        """Close every socket that authenticated with *token* and announce the
        user offline now, rather than after the client's close handshake."""
        for websocket in self.connection_manager.get_websockets_for_token(token):
            await close_quietly(websocket, WS_CLOSE_UNAUTHENTICATED)
            await self.remove_connection_by_websocket(websocket)

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

        if isinstance(frame, PingFrame):
            await send_frame(
                websocket, {"type": "pong", "t": frame.t}, self._drop_stalled)
        elif isinstance(frame, ConnectionInitFrame):
            # Legacy frame: registration now happens at handshake. Older
            # frontends still send it, so answer with a fresh presence snapshot
            # instead of erroring.
            await self._send_presence_snapshot(user_id, websocket)
        elif isinstance(frame, DisconnectFrame):
            await self.remove_connection_by_websocket(websocket)
        elif isinstance(frame, MessageFrame):
            await self.chat_service.handle_message(user_id, frame, websocket)
        elif isinstance(frame, DirectMessageFrame):
            await self.chat_service.handle_direct_message(user_id, frame, websocket)
        elif isinstance(frame, TypingFrame):
            await self.chat_service.handle_typing(user_id, frame)
        elif isinstance(frame, ActivityFrame):
            active = frame.state == "active"
            self.connection_manager.set_activity(websocket, active)
            if active:
                await touch_last_active(user_id)

    async def reject_frame(self, websocket: WebSocket, reason: str) -> None:
        """Answer a frame that could not even be decoded; the socket stays open."""
        logger.warning("Rejected frame from user %s: %s",
                       self.connection_manager.get_user_id(websocket), reason)
        await self._send_error(websocket, "invalid_frame", None)

    async def _send_error(
        self, websocket: WebSocket, code: str, ref: str | None
    ) -> None:
        await send_frame(
            websocket, {"type": "error", "code": code, "ref": ref}, self._drop_stalled)

    @staticmethod
    async def get_related_user_ids(user_id: int) -> set[int]:
        """Return the IDs of users who share a server with *user_id*, excluding
        *user_id* itself (presence relies on that; add the user back if needed).
        """
        user_server_ids = await UserToServer.filter(
            user_id=user_id
        ).values_list("server_id", flat=True)

        return set(
            await UserToServer.filter(
                server_id__in=user_server_ids
            ).values_list("user_id", flat=True)
        ) - {user_id}

    @staticmethod
    async def get_conversation_partner_ids(user_id: int) -> set[int]:
        """Return the IDs of users who have a direct-message conversation with
        *user_id*, whether or not it has any messages."""
        pairs = await Conversation.filter(
            Q(user_a_id=user_id) | Q(user_b_id=user_id)
        ).values_list("user_a_id", "user_b_id")
        return {uid for pair in pairs for uid in pair} - {user_id}

    async def get_presence_audience_ids(self, user_id: int) -> set[int]:
        """Return the IDs of users who see *user_id*'s presence and whose
        presence *user_id* sees: server co-members, direct-message partners and
        friends."""
        related, partners, friends = await asyncio.gather(
            self.get_related_user_ids(user_id),
            self.get_conversation_partner_ids(user_id),
            friend_ids(user_id),
        )
        return related | partners | friends

    async def announce_presence_between(self, user_a_id: int, user_b_id: int) -> None:
        """Tell each of two users who just became partners that the other is
        online. Nothing is sent for an offline user: offline is the default."""
        for user_id, other_id in ((user_a_id, user_b_id), (user_b_id, user_a_id)):
            if self.connection_manager.is_online(other_id):
                await self.send_to_user(user_id, {
                    "type": "presence_update", "user_id": other_id, "online": True})

    async def _send_presence_snapshot(
        self,
        user_id: int,
        websocket: WebSocket,
        *,
        audience_ids: set[int] | None = None,
    ) -> None:
        """Send *websocket* the presence-audience users who are currently online."""
        if audience_ids is None:
            audience_ids = await self.get_presence_audience_ids(user_id)
        await send_frame(websocket, {
            "type": "presence_init",
            "user_ids": [
                uid for uid in audience_ids
                if self.connection_manager.is_online(uid)
            ],
        }, self._drop_stalled)

    async def _send_permissions_snapshot(self, user_id: int, websocket: WebSocket) -> None:
        """Send *websocket* the user's permission mask for every server they are
        in, and their masks in channels where those differ."""
        servers = await Server.filter(server_users__user_id=user_id)
        masks, channels = {}, {}
        for server in servers:
            view = await permissions.view(user_id, server)
            if view is not None:
                masks[str(server.id)] = str(int(view.mask))
                channels[str(server.id)] = view.differing_masks()
        await send_frame(
            websocket,
            {"type": "permissions_init", "servers": masks, "channels": channels},
            self._drop_stalled)

    async def _notify_presence(
        self,
        user_id: int,
        *,
        online: bool,
        websocket: WebSocket | None = None,
        was_online: bool = False,
    ) -> None:
        """Handle presence notifications on connect/disconnect.

        When *online* is True (user just connected):
          1. Send the connecting socket its own presence_init snapshot,
             regardless of how many other sockets this user already has.
          2. If this is the user's first socket (0 -> 1), notify the online
             users in the presence audience that this user came online. A
             second tab connecting must not re-announce someone who is
             already online.

        When *online* is False (user disconnected):
          Notify the online users in the presence audience that this user
          went offline. The caller only calls this once the user has no
          remaining sockets.
        """
        audience_ids = await self.get_presence_audience_ids(user_id)

        if online:
            if websocket is not None:
                await self._send_presence_snapshot(
                    user_id, websocket, audience_ids=audience_ids)
            if was_online:
                return

        # Broadcast the status change to the audience, on every socket they
        # have open.
        status_message = {
            "type": "presence_update",
            "user_id": user_id,
            "online": online,
        }

        await send_frame_to_all(
            self.connection_manager.websockets_of(audience_ids),
            status_message,
            self._drop_stalled,
        )

    async def broadcast_to_server(
        self, server_id: int, frame: dict, *, exclude_user_id: int | None = None
    ) -> None:
        """Send *frame* to every connected member of *server_id*.

        Used for server-scoped events (new channel, new member). One dead
        recipient never stops delivery to the rest.
        """
        member_ids = await UserToServer.filter(
            server_id=server_id).values_list("user_id", flat=True)
        await self.send_to_users(member_ids, frame, exclude_user_id=exclude_user_id)

    async def broadcast_to_channel(
        self, server_id: int, channel_id: int, frame: dict,
        *, exclude_user_id: int | None = None,
    ) -> None:
        """Send *frame* to every connected member who can view the channel."""
        await self.send_to_users(
            await permissions.viewers(server_id, channel_id), frame,
            exclude_user_id=exclude_user_id)

    async def send_to_users(
        self, user_ids, frame: dict, *, exclude_user_id: int | None = None
    ) -> None:
        """Send *frame* to every socket of each connected user in *user_ids*,
        skipping *exclude_user_id* entirely. One dead socket never stops
        delivery to the rest."""
        await send_frame_to_all(
            self.connection_manager.websockets_of(
                user_ids, exclude_user_id=exclude_user_id),
            frame,
            self._drop_stalled,
        )

    async def send_to_user(self, user_id: int, frame: dict) -> None:
        """Send *frame* to every socket of a single user (e.g. a REST-triggered
        update like read_state, where the caller can't tell which tab acted)."""
        await self.send_to_users([user_id], frame)

    async def notify_user_invalidate(self, user_id: int):
        """Notify related users that a user's profile has changed.

        Sends a lightweight invalidation message (just the user_id) to the
        user's presence audience (server co-members, direct-message partners and
        friends), to users with a pending friend request to or from them, and to
        the user's own sockets so their other tabs update too. Clients should
        fetch the updated profile via REST if they need the new data.
        """
        recipients = (
            await self.get_presence_audience_ids(user_id)
            | await pending_counterpart_ids(user_id)
            | {user_id}
        )
        await self.send_to_users(recipients, {"type": "user_invalidate", "user_id": user_id})
