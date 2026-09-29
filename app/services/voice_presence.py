import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from livekit import api
from livekit.protocol import models

from app.models.Channel import Channel

logger = logging.getLogger("app.voice_presence")

POLL_INTERVAL_SECONDS = 3.0
MAX_BACKOFF_SECONDS = 60.0
ROOM_PREFIX = "channel_"

_MIC_SOURCES = (models.TrackSource.MICROPHONE, models.TrackSource.UNKNOWN)


@dataclass(frozen=True)
class VoiceParticipant:
    user_id: int
    muted: bool
    deafened: bool

    def to_json(self) -> dict:
        return {
            "user_id": self.user_id,
            "muted": self.muted,
            "deafened": self.deafened,
        }


Snapshot = dict[int, tuple[VoiceParticipant, ...]]
Notify = Callable[[int, tuple[VoiceParticipant, ...]], Awaitable[None]]


def participant_from_info(info) -> VoiceParticipant | None:
    if info.state == models.ParticipantInfo.State.DISCONNECTED:
        return None
    try:
        user_id = int(info.identity)
    except ValueError:
        return None

    has_live_mic = any(
        t.type == models.TrackType.AUDIO
        and t.source in _MIC_SOURCES
        and not t.muted
        for t in info.tracks
    )
    return VoiceParticipant(
        user_id=user_id,
        muted=not has_live_mic,
        deafened=info.attributes.get("deafened") == "true",
    )


class LiveKitRoomSource:
    """Reads voice channel occupancy from LiveKit. The only code that touches
    the LiveKit SDK."""

    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self._credentials = (url, api_key, api_secret)
        self._client: api.LiveKitAPI | None = None

    def _api(self) -> api.LiveKitAPI:
        # Built on first use so the HTTP session is created inside the running loop.
        if self._client is None:
            self._client = api.LiveKitAPI(*self._credentials)
        return self._client

    async def fetch(self) -> Snapshot:
        rooms = await self._api().room.list_rooms(api.ListRoomsRequest())
        snapshot: Snapshot = {}
        for room in rooms.rooms:
            channel_id = _channel_id_from_room(room.name)
            if channel_id is None or room.num_participants <= 0:
                continue
            listed = await self._api().room.list_participants(
                api.ListParticipantsRequest(room=room.name))
            participants = sorted(
                filter(None, map(participant_from_info, listed.participants)),
                key=lambda p: p.user_id,
            )
            if participants:
                snapshot[channel_id] = tuple(participants)
        return snapshot

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _channel_id_from_room(name: str) -> int | None:
    if not name.startswith(ROOM_PREFIX):
        return None
    try:
        return int(name[len(ROOM_PREFIX):])
    except ValueError:
        return None


class VoicePresence:
    """Keeps who is in each voice channel in memory and reports changes."""

    def __init__(
        self,
        source,
        notify: Notify,
        *,
        interval: float = POLL_INTERVAL_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
        sleep=asyncio.sleep,
    ) -> None:
        self._source = source
        self._notify = notify
        self._interval = interval
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._snapshot: Snapshot = {}

    def channel_participants(self, channel_id: int) -> tuple[VoiceParticipant, ...]:
        return self._snapshot.get(channel_id, ())

    async def apply_snapshot(self, snapshot: Snapshot) -> None:
        for channel_id in self._snapshot.keys() | snapshot.keys():
            participants = snapshot.get(channel_id, ())
            if participants == self._snapshot.get(channel_id, ()):
                continue
            try:
                await self._notify(channel_id, participants)
            except Exception:
                logger.warning(
                    "Failed to notify voice state for channel %s",
                    channel_id, exc_info=True)
        self._snapshot = snapshot

    async def poll_once(self) -> None:
        await self.apply_snapshot(await self._source.fetch())

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
            except Exception:
                if failures == 0:
                    logger.warning("Voice presence poll failed", exc_info=True)
                else:
                    logger.debug("Voice presence poll failed again", exc_info=True)
                delay = min(self._interval * 2 ** min(failures, 30), self._max_backoff)
                failures += 1
            else:
                if failures:
                    logger.info("Voice presence poll recovered after %s failure(s)", failures)
                    failures = 0
                delay = self._interval
            await self._sleep(delay)

    async def aclose(self) -> None:
        await self._source.aclose()


def livekit_api_url() -> str | None:
    explicit = os.getenv("LIVEKIT_API_URL")
    if explicit:
        return explicit
    public = os.getenv("LIVEKIT_URL")
    if not public:
        return None
    if public.startswith("wss://"):
        return "https://" + public[len("wss://"):]
    if public.startswith("ws://"):
        return "http://" + public[len("ws://"):]
    return public


def make_broadcast_notify(comms) -> Notify:
    async def notify(channel_id: int, participants: tuple[VoiceParticipant, ...]) -> None:
        channel = await Channel.get_or_none(id=channel_id)
        if channel is None:
            return  # channel was deleted
        await comms.broadcast_to_server(channel.server_id, {
            "type": "voice_state",
            "server_id": channel.server_id,
            "channel_id": channel_id,
            "participants": [p.to_json() for p in participants],
        })

    return notify


def create_voice_presence(comms) -> VoicePresence | None:
    url = livekit_api_url()
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and api_key and api_secret):
        logger.info("LiveKit not configured; voice presence disabled")
        return None
    return VoicePresence(
        LiveKitRoomSource(url, api_key, api_secret),
        make_broadcast_notify(comms),
    )
