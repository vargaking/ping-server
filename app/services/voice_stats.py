import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Literal

import aiohttp
from livekit import api
from livekit.protocol import models

from app.services.voice_presence import livekit_api_url

logger = logging.getLogger("app.voice_stats")


@dataclass(frozen=True)
class VoiceStats:
    rooms: int
    participants: int
    screenshares: list[dict[str, int]]


VoiceStatus = Literal["ok", "unconfigured", "unreachable"]

VOICE_STATS_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class VoiceReading:
    status: VoiceStatus
    stats: VoiceStats | None = None


_logged_failure = False


async def voice_snapshot() -> VoiceReading:
    """Aggregate LiveKit usage, telling an unconfigured server apart from an
    unreachable one. Carries no user ids or room names."""
    global _logged_failure
    url = livekit_api_url()
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        return VoiceReading("unconfigured")

    client = api.LiveKitAPI(
        url, key, secret, timeout=aiohttp.ClientTimeout(total=VOICE_STATS_TIMEOUT_SECONDS))
    try:
        async with asyncio.timeout(VOICE_STATS_TIMEOUT_SECONDS):
            rooms = (await client.room.list_rooms(api.ListRoomsRequest())).rooms
            participants = 0
            screenshares: list[dict[str, int]] = []
            for room in rooms:
                listed = await client.room.list_participants(
                    api.ListParticipantsRequest(room=room.name))
                participants += len(listed.participants)
                for participant in listed.participants:
                    for track in participant.tracks:
                        if track.source == models.TrackSource.SCREEN_SHARE:
                            screenshares.append({"width": track.width, "height": track.height})
    except Exception:
        if not _logged_failure:
            logger.warning("Voice stats unavailable", exc_info=True)
            _logged_failure = True
        return VoiceReading("unreachable")
    finally:
        await client.aclose()
    _logged_failure = False
    return VoiceReading("ok", VoiceStats(len(rooms), participants, screenshares))
