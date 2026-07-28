import json
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from livekit import api
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.User import User
from ..utils import require_membership

router = APIRouter(prefix="/api/voice", tags=["voice"])

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# How long a freshly minted join token stays valid. The client only needs it
# long enough to open the LiveKit connection; the session persists after that.
TOKEN_TTL_SECONDS = 300


class VoiceTokenRequest(BaseModel):
    channel_id: int


class VoiceTokenResponse(BaseModel):
    token: str
    url: str
    room: str


@router.post("/token", response_model=VoiceTokenResponse)
async def create_voice_token(
    body: VoiceTokenRequest,
    current_user: User = Depends(get_current_user),
) -> VoiceTokenResponse:
    """Mint a LiveKit access token for a voice channel.

    Verifies the user is a member of the channel's server before issuing a
    token scoped to that one room. LiveKit itself never touches our DB.
    """
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice service is not configured",
        )

    channel = await Channel.get_or_none(id=body.channel_id).prefetch_related("server")
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    if channel.type != "voice":
        raise HTTPException(status_code=400, detail="Channel is not a voice channel")

    # 403 if the user isn't a member of the server this channel belongs to.
    await require_membership(current_user, channel.server)

    room = f"channel_{channel.id}"

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
    )
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(str(current_user.id))
        .with_name(current_user.username)
        # Carry the user's profile so peers can render avatars without a lookup.
        .with_metadata(json.dumps(current_user.profile or {}))
        .with_ttl(timedelta(seconds=TOKEN_TTL_SECONDS))
        .with_grants(grant)
        .to_jwt()
    )

    return VoiceTokenResponse(token=token, url=LIVEKIT_URL, room=room)
