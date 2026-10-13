"""Game voice: the sessions a game server drives, and the codes players link with.

A session lives in memory for as long as its integration keeps sending frames.
The game computes per-listener gains; this module translates game player ids to
user ids, tells channel viewers who is playing, and forwards each listener only
their own gains. The app runs one process, so this state is authoritative.
"""
import asyncio
import hashlib
import logging
import secrets
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..models.GameIntegration import GameIntegration
from ..models.GameLink import GameLink
from ..models.Server import Server
from ..permissions import Permission
from .permissions import permissions

logger = logging.getLogger("app.game_sessions")

KEY_PREFIX = "zgk_"
IDLE_SECONDS = 10.0
SWEEP_INTERVAL_SECONDS = 2.0
RESEND_SECONDS = 2.0
# Hundredths of gain: a change counts when it is more than this.
GAIN_CHANGE_CENTS = 2
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 600.0

Clock = Callable[[], float]


class NoVoiceChannel(Exception):
    """The integration has no voice channel to drive."""


class IntegrationChanged(Exception):
    """The integration was deleted or got a new key while a frame was in flight."""


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_prefix(key: str) -> str:
    return key[len(KEY_PREFIX):len(KEY_PREFIX) + 8]


def bearer_token(header: str | None) -> str | None:
    scheme, _, token = (header or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


@dataclass(frozen=True)
class LinkedPlayer:
    user_id: int
    player_name: str


@dataclass
class Session:
    integration_id: int
    server_id: int
    channel_id: int
    name: str
    players_hear_spectators: bool
    last_frame: float
    players: dict[int, str] = field(default_factory=dict)
    # Per listener: gains in hundredths last sent, and when.
    sent: dict[int, tuple[dict[int, int], float]] = field(default_factory=dict)

    def body(self, *, active: bool = True) -> dict:
        return {
            "type": "game_session",
            "server_id": self.server_id,
            "channel_id": self.channel_id,
            "integration_id": self.integration_id,
            "name": self.name,
            "active": active,
            "players_hear_spectators": self.players_hear_spectators,
            "players": [
                {"user_id": user_id, "player_name": name}
                for user_id, name in sorted(self.players.items())
            ] if active else [],
        }


def _cents(gain: float) -> int:
    if gain != gain:
        return 0
    return round(min(1.0, max(0.0, gain)) * 100)


def _differs(old: Mapping[int, int], new: Mapping[int, int]) -> bool:
    if old.keys() != new.keys():
        return True
    return any(abs(new[user_id] - old[user_id]) > GAIN_CHANGE_CENTS for user_id in new)


class GameSessions:
    def __init__(self, comms, clock: Clock = time.monotonic) -> None:
        self._comms = comms
        self.clock = clock
        self._sessions: dict[int, Session] = {}
        self._links: dict[int, dict[str, LinkedPlayer]] = {}
        self._links_stale: set[int] = set()
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, integration_id: int) -> asyncio.Lock:
        lock = self._locks.get(integration_id)
        if lock is None:
            lock = self._locks[integration_id] = asyncio.Lock()
        return lock

    def is_active(self, integration_id: int) -> bool:
        return integration_id in self._sessions

    def session_for_channel(self, channel_id: int) -> dict | None:
        for session in self._sessions.values():
            if session.channel_id == channel_id:
                return session.body()
        return None

    def links_changed(self, integration_id: int) -> None:
        self._links.pop(integration_id, None)
        self._links_stale.add(integration_id)

    async def _link_map(self, integration_id: int) -> dict[str, LinkedPlayer]:
        cached = self._links.get(integration_id)
        if cached is not None:
            return cached
        self._links_stale.discard(integration_id)
        rows = await GameLink.filter(integration_id=integration_id).values_list(
            "player_id", "user_id", "player_name")
        links = {
            player_id: LinkedPlayer(user_id, player_name)
            for player_id, user_id, player_name in rows
        }
        if integration_id not in self._links_stale:
            self._links[integration_id] = links
        return links

    @staticmethod
    async def _linked(
        integration: GameIntegration, players: Sequence[str], links: Mapping[str, LinkedPlayer],
    ) -> list[str]:
        """The players with a link whose user can still view the channel. Checked on
        every frame (the masks are cached), so a kick, a leave or a hidden channel
        takes effect at once and a rejoin needs no relink."""
        known = [player_id for player_id in players if player_id in links]
        server = await Server.get_or_none(id=integration.server_id) if known else None
        linked = []
        for player_id in known if server else ():
            mask = await permissions.effective(
                links[player_id].user_id, server, channel_id=integration.voice_channel_id)
            if mask is not None and mask & Permission.VIEW_CHANNEL:
                linked.append(player_id)
        return linked

    async def frame(
        self,
        integration: GameIntegration,
        players: Sequence[str],
        mix: Mapping[str, Mapping[str, float]],
    ) -> list[str]:
        """Apply one frame from the game and return which of *players* are linked.

        The integration is read again under the lock: a delete, a key rotation
        or a channel change that landed while the frame was in flight must not
        bring a session back.
        """
        async with self._lock(integration.id):
            current = await GameIntegration.get_or_none(id=integration.id)
            if current is None or current.key_hash != integration.key_hash:
                raise IntegrationChanged
            if current.voice_channel_id is None:
                await self._end(current.id)
                raise NoVoiceChannel
            integration = current
            now = self.clock()
            links = await self._link_map(integration.id)
            linked = await self._linked(integration, players, links)
            roster = {links[player_id].user_id: links[player_id].player_name
                      for player_id in linked}

            session = self._sessions.get(integration.id)
            if session is not None and session.channel_id != integration.voice_channel_id:
                await self._end(integration.id)
                session = None
            changed = session is None
            if session is None:
                session = self._sessions[integration.id] = Session(
                    integration_id=integration.id,
                    server_id=integration.server_id,
                    channel_id=integration.voice_channel_id,
                    name=integration.name,
                    players_hear_spectators=integration.players_hear_spectators,
                    last_frame=now,
                )
            session.last_frame = now

            if (roster != session.players or session.name != integration.name
                    or session.players_hear_spectators != integration.players_hear_spectators):
                changed = True
                session.players = roster
                session.name = integration.name
                session.players_hear_spectators = integration.players_hear_spectators
            if changed:
                await self._announce(session)

            await self._send_mixes(session, links, linked, mix, now)
            return linked

    async def _send_mixes(
        self,
        session: Session,
        links: Mapping[str, LinkedPlayer],
        linked: Sequence[str],
        mix: Mapping[str, Mapping[str, float]],
        now: float,
    ) -> None:
        listening = {links[player_id].user_id for player_id in linked}
        for user_id in set(session.sent) - listening:
            del session.sent[user_id]

        sends = []
        for listener_id in linked:
            listener = links[listener_id].user_id
            heard = mix.get(listener_id, {})
            gains = {}
            for speaker_id in linked:
                if speaker_id == listener_id or speaker_id not in heard:
                    continue
                cents = _cents(heard[speaker_id])
                if cents:
                    gains[links[speaker_id].user_id] = cents

            previous = session.sent.get(listener)
            if (previous is not None and now - previous[1] < RESEND_SECONDS
                    and not _differs(previous[0], gains)):
                continue
            session.sent[listener] = (gains, now)
            sends.append(self._send(listener, {
                "type": "game_mix",
                "channel_id": session.channel_id,
                "integration_id": session.integration_id,
                "gains": {str(user_id): cents / 100 for user_id, cents in gains.items()},
            }))
        await asyncio.gather(*sends)

    async def end(self, integration_id: int) -> None:
        async with self._lock(integration_id):
            await self._end(integration_id)

    async def _end(self, integration_id: int) -> None:
        session = self._sessions.pop(integration_id, None)
        self._links.pop(integration_id, None)
        if session is not None:
            await self._broadcast(session.server_id, session.channel_id,
                                  session.body(active=False))

    async def settings_changed(self, integration: GameIntegration) -> None:
        async with self._lock(integration.id):
            session = self._sessions.get(integration.id)
            if session is None:
                return
            session.name = integration.name
            session.players_hear_spectators = integration.players_hear_spectators
            await self._announce(session)

    async def _announce(self, session: Session) -> None:
        await self._broadcast(session.server_id, session.channel_id, session.body())

    async def _broadcast(self, server_id: int, channel_id: int, body: dict) -> None:
        if self._comms is not None:
            await self._comms.broadcast_to_channel(server_id, channel_id, body)

    async def _send(self, user_id: int, body: dict) -> None:
        if self._comms is not None:
            await self._comms.send_to_user(user_id, body)

    async def sweep(self) -> None:
        """End the sessions that have had no frame for IDLE_SECONDS."""
        for integration_id in list(self._sessions):
            async with self._lock(integration_id):
                session = self._sessions.get(integration_id)
                if session is not None and self.clock() - session.last_frame >= IDLE_SECONDS:
                    await self._end(integration_id)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await self.sweep()
            except Exception:
                logger.warning("Game session sweep failed", exc_info=True)


@dataclass(frozen=True)
class LinkCode:
    integration_id: int
    player_id: str
    player_name: str
    expires_at: float


class LinkCodes:
    """Pending link codes. One per (integration, player id); the newest wins."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        # Insertion order is expiry order, as every code lives the same time.
        self._codes: dict[str, LinkCode] = {}
        self._by_player: dict[tuple[int, str], str] = {}

    @staticmethod
    def normalize(code: str) -> str:
        return code.strip().upper()

    def _purge(self) -> None:
        now = self.clock()
        while self._codes:
            code = next(iter(self._codes))
            if self._codes[code].expires_at > now:
                return
            self._drop(code)

    def _drop(self, code: str) -> LinkCode | None:
        entry = self._codes.pop(code, None)
        if entry is not None:
            self._by_player.pop((entry.integration_id, entry.player_id), None)
        return entry

    def create(self, integration_id: int, player_id: str, player_name: str) -> str:
        self._purge()
        previous = self._by_player.get((integration_id, player_id))
        if previous is not None:
            self._drop(previous)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        while code in self._codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._codes[code] = LinkCode(
            integration_id, player_id, player_name, self.clock() + CODE_TTL_SECONDS)
        self._by_player[(integration_id, player_id)] = code
        return code

    def get(self, code: str) -> LinkCode | None:
        self._purge()
        return self._codes.get(self.normalize(code))

    def consume(self, code: str) -> LinkCode | None:
        self._purge()
        return self._drop(self.normalize(code))

    def forget_integration(self, integration_id: int) -> None:
        for code in [c for c, e in self._codes.items() if e.integration_id == integration_id]:
            self._drop(code)
