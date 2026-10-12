import asyncio
import logging
from typing import Protocol

import aiohttp
from livekit import api

from app.services import voice_presence
from app.services.voice_presence import livekit_api_url, participant_from_info

logger = logging.getLogger("app.services.call_rooms")

ROOM_PREFIX = "call_"


def room_name(call_id: int) -> str:
    return f"{ROOM_PREFIX}{call_id}"


class CallRooms(Protocol):
    async def participants(self, call_id: int) -> frozenset[int]:
        """Who is in the call's room. A missing room is empty; other failures raise."""

    async def remove_participant(self, call_id: int, user_id: int) -> None: ...

    async def delete_room(self, call_id: int) -> None: ...

    async def aclose(self) -> None: ...


def _is_not_found(error: api.ServerError) -> bool:
    return error.code == api.ServerErrorCode.NOT_FOUND


class LiveKitCallRooms:
    """Reads and closes call rooms on LiveKit. The presence poller ignores
    rooms that are not channels, so calls need their own adapter."""

    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self._credentials = (url, api_key, api_secret)
        self._client: api.LiveKitAPI | None = None

    def _api(self) -> api.LiveKitAPI:
        # Built on first use so the HTTP session is created inside the running loop.
        if self._client is None:
            self._client = api.LiveKitAPI(
                *self._credentials,
                timeout=aiohttp.ClientTimeout(total=voice_presence.LIVEKIT_TIMEOUT_SECONDS))
        return self._client

    async def participants(self, call_id: int) -> frozenset[int]:
        try:
            async with asyncio.timeout(voice_presence.READ_TIMEOUT_SECONDS):
                listed = await self._api().room.list_participants(
                    api.ListParticipantsRequest(room=room_name(call_id)))
        except api.ServerError as error:
            if _is_not_found(error):
                return frozenset()
            raise
        found = filter(None, map(participant_from_info, listed.participants))
        return frozenset(p.user_id for p in found)

    async def remove_participant(self, call_id: int, user_id: int) -> None:
        try:
            await self._api().room.remove_participant(api.RoomParticipantIdentity(
                room=room_name(call_id), identity=str(user_id)))
        except api.ServerError as error:
            if not _is_not_found(error):
                raise

    async def delete_room(self, call_id: int) -> None:
        try:
            await self._api().room.delete_room(api.DeleteRoomRequest(room=room_name(call_id)))
        except api.ServerError as error:
            if not _is_not_found(error):
                raise

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def create_call_rooms() -> LiveKitCallRooms | None:
    # Imported here because the voice router is what the tests configure LiveKit on.
    from app.routers import voice
    url = livekit_api_url()
    key, secret = voice.LIVEKIT_API_KEY, voice.LIVEKIT_API_SECRET
    return LiveKitCallRooms(url, key, secret) if url and key and secret else None
