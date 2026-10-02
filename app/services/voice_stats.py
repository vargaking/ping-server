import logging
import os
from dataclasses import dataclass

from livekit import api
from livekit.protocol import models

from app.services.voice_presence import livekit_api_url

logger = logging.getLogger("app.voice_stats")


@dataclass(frozen=True)
class VoiceStats:
    rooms: int
    participants: int
    screenshares: list[dict[str, int]]


_logged_failure = False


async def voice_snapshot() -> VoiceStats | None:
    """Aggregate LiveKit usage, or None when LiveKit is not configured or
    unreachable. Carries no user ids or room names."""
    global _logged_failure
    url = livekit_api_url()
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        return None

    client = api.LiveKitAPI(url, key, secret)
    try:
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
        return None
    finally:
        await client.aclose()
    _logged_failure = False
    return VoiceStats(len(rooms), participants, screenshares)
