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
    server_muted: bool = False

    def to_json(self) -> dict:
        return {
            "user_id": self.user_id,
            "muted": self.muted,
            "deafened": self.deafened,
            "server_muted": self.server_muted,
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
        server_muted=info.attributes.get("server_muted") == "true",
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
            # num_participants lags behind joins by several seconds, so every
            # channel room is listed rather than trusting it.
            if channel_id is None:
                continue
            if participants := await self.fetch_channel(channel_id):
                snapshot[channel_id] = participants
        return snapshot

    async def fetch_channel(self, channel_id: int) -> tuple[VoiceParticipant, ...]:
        listed = await self._api().room.list_participants(
            api.ListParticipantsRequest(room=f"{ROOM_PREFIX}{channel_id}"))
        return tuple(sorted(
            filter(None, map(participant_from_info, listed.participants)),
            key=lambda p: p.user_id,
        ))

    async def remove_participant(self, channel_id: int, user_id: int) -> None:
        await self._api().room.remove_participant(api.RoomParticipantIdentity(
            room=f"{ROOM_PREFIX}{channel_id}", identity=str(user_id)))

    async def update_participant(
        self, channel_id: int, user_id: int, sources: list[str], attributes: dict[str, str]
    ) -> None:
        permission = models.ParticipantPermission(
            can_subscribe=True,
            # An empty source list means "any source" to LiveKit.
            can_publish=bool(sources),
            can_publish_sources=[models.TrackSource.Value(s.upper()) for s in sources],
            can_publish_data=True,
            can_update_metadata=True,
        )
        await self._api().room.update_participant(api.UpdateParticipantRequest(
            room=f"{ROOM_PREFIX}{channel_id}", identity=str(user_id),
            permission=permission, attributes=attributes))

    async def delete_room(self, channel_id: int) -> None:
        await self._api().room.delete_room(
            api.DeleteRoomRequest(room=f"{ROOM_PREFIX}{channel_id}"))

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
    """Keeps who is in each voice channel in memory and reports changes.

    A background poll catches everything eventually; refresh() re-reads one
    channel right away when a client reports a change there.
    """

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
        # Every read of LiveKit takes a number when it starts. A channel only
        # accepts reads that started after the one it last applied, so a slow
        # full poll can't overwrite a newer single-channel refresh.
        self._read_seq = 0
        self._applied_seq: dict[int, int] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._refreshing: dict[int, asyncio.Task] = {}
        self._refresh_again: set[int] = set()

    def channel_participants(self, channel_id: int) -> tuple[VoiceParticipant, ...]:
        return self._snapshot.get(channel_id, ())

    def occupancy(self) -> dict[int, int]:
        """How many people are in each occupied voice channel."""
        return {channel_id: len(people) for channel_id, people in self._snapshot.items()}

    def channels_of(self, user_id: int) -> list[int]:
        return sorted(
            channel_id
            for channel_id, participants in self._snapshot.items()
            if any(p.user_id == user_id for p in participants)
        )

    def _next_read(self) -> int:
        self._read_seq += 1
        return self._read_seq

    async def apply_snapshot(self, snapshot: Snapshot, read: int | None = None) -> None:
        read = self._next_read() if read is None else read
        for channel_id in self._snapshot.keys() | snapshot.keys():
            await self._apply_channel(channel_id, snapshot.get(channel_id, ()), read)

    async def _apply_channel(
        self, channel_id: int, participants: tuple[VoiceParticipant, ...], read: int
    ) -> None:
        # Held across notify so frames for one channel go out in read order.
        async with self._channel_locks.setdefault(channel_id, asyncio.Lock()):
            if read <= self._applied_seq.get(channel_id, 0):
                return
            self._applied_seq[channel_id] = read
            if participants == self._snapshot.get(channel_id, ()):
                return
            if participants:
                self._snapshot[channel_id] = participants
            else:
                self._snapshot.pop(channel_id, None)
            try:
                await self._notify(channel_id, participants)
            except Exception:
                logger.warning(
                    "Failed to notify voice state for channel %s",
                    channel_id, exc_info=True)

    async def poll_once(self) -> None:
        read = self._next_read()
        await self.apply_snapshot(await self._source.fetch(), read)

    def refresh(self, channel_id: int) -> None:
        """Re-read one channel soon. Calls while a read of that channel is in
        flight fold into a single follow-up read."""
        if channel_id in self._refreshing:
            self._refresh_again.add(channel_id)
            return
        self._refreshing[channel_id] = asyncio.create_task(self._refresh_loop(channel_id))

    async def _refresh_loop(self, channel_id: int) -> None:
        try:
            while True:
                self._refresh_again.discard(channel_id)
                read = self._next_read()
                try:
                    participants = await self._source.fetch_channel(channel_id)
                except Exception:
                    logger.debug("Voice channel %s refresh failed", channel_id, exc_info=True)
                    return
                await self._apply_channel(channel_id, participants, read)
                if channel_id not in self._refresh_again:
                    return
        finally:
            self._refreshing.pop(channel_id, None)

    async def remove_participant(self, channel_id: int, user_id: int) -> None:
        """Disconnect a user from one voice channel. Missing rooms or
        participants are fine: most channels won't have them."""
        try:
            await self._source.remove_participant(channel_id, user_id)
        except Exception:
            logger.debug(
                "No voice participant %s to remove from channel %s",
                user_id, channel_id, exc_info=True)
            return
        self.refresh(channel_id)

    async def update_participant(
        self, channel_id: int, user_id: int, sources: list[str], attributes: dict[str, str]
    ) -> None:
        """Change what one user may publish in a voice channel, and their
        attributes. A missing participant is fine."""
        try:
            await self._source.update_participant(channel_id, user_id, sources, attributes)
        except Exception:
            logger.debug(
                "No voice participant %s to update in channel %s",
                user_id, channel_id, exc_info=True)
            return
        self.refresh(channel_id)

    async def close_channel(self, channel_id: int) -> None:
        """Disconnect everyone in a voice channel that is being deleted and
        forget its participants. A missing room is fine: most channels are empty."""
        try:
            await self._source.delete_room(channel_id)
        except Exception:
            logger.debug("No voice room to delete for channel %s", channel_id, exc_info=True)
        async with self._channel_locks.setdefault(channel_id, asyncio.Lock()):
            # Reads that started before now must not bring the participants back.
            self._applied_seq[channel_id] = self._next_read()
            self._snapshot.pop(channel_id, None)

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
        tasks = list(self._refreshing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._source.aclose()


async def remove_from_voice(presence: VoicePresence | None, channel_ids, user_id: int) -> None:
    """Pull a user out of these voice channels, e.g. after they leave the server."""
    if presence is None:
        return
    for channel_id in channel_ids:
        await presence.remove_participant(channel_id, user_id)


async def voice_channels_of(
    presence: VoicePresence | None, server_id: int, user_id: int
) -> list[int]:
    """The voice channels of *server_id* the user is currently in."""
    if presence is None:
        return []
    in_voice = presence.channels_of(user_id)
    if not in_voice:
        return []
    server_channels = await Channel.filter(
        server_id=server_id, type="voice", id__in=in_voice).values_list("id", flat=True)
    return list(server_channels)


async def close_voice_channels(presence: VoicePresence | None, channel_ids) -> None:
    """Shut down the rooms of voice channels that were deleted."""
    if presence is None:
        return
    for channel_id in channel_ids:
        await presence.close_channel(channel_id)


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
