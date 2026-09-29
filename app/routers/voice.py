import json
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from livekit import api
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Server import Server
from ..models.User import User
from ..permissions import Permission, require_permission

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

    Verifies the user may connect to voice in the channel's server before
    issuing a token scoped to that one room. LiveKit itself never touches our DB.
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

    # 403 if the user isn't a member of the server this channel belongs to, or
    # can't connect. SPEAK and STREAM aren't enforced in the token yet.
    await require_permission(current_user, channel.server, Permission.CONNECT)

    room = f"channel_{channel.id}"

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        # Lets the client set participant attributes, which is how it shares
        # deafen state with the room (mute travels as track mute events).
        can_update_own_metadata=True,
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


class VoicePresenceParticipant(BaseModel):
    user_id: int
    muted: bool
    deafened: bool


class VoicePresenceChannel(BaseModel):
    channel_id: int
    participants: list[VoicePresenceParticipant]


@router.get("/presence/{server_id}", response_model=list[VoicePresenceChannel])
async def get_voice_presence(
    server_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[VoicePresenceChannel]:
    """Who is currently in each voice channel of a server, for members.

    Clients call this when they open a server; voice_state frames keep it
    current afterwards. Empty when voice presence is not running.
    """
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_permission(current_user, server, Permission(0))

    presence = getattr(request.app.state, "voice_presence", None)
    if presence is None:
        return []

    channel_ids = await Channel.filter(server_id=server_id).values_list("id", flat=True)
    return [
        VoicePresenceChannel(
            channel_id=channel_id,
            participants=[
                VoicePresenceParticipant(**p.to_json()) for p in participants
            ],
        )
        for channel_id in channel_ids
        if (participants := presence.channel_participants(channel_id))
    ]


@router.post("/presence/channels/{channel_id}/refresh", status_code=204)
async def refresh_voice_presence(
    channel_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Re-read a voice channel's occupancy from LiveKit now.

    Clients call this right after they join, leave, mute or deafen, so other
    members see it without waiting for the next poll. What gets broadcast
    always comes from LiveKit, never from the caller.
    """
    channel = await Channel.get_or_none(id=channel_id).prefetch_related("server")
    if not channel or channel.type != "voice":
        raise HTTPException(status_code=404, detail="Voice channel not found")
    await require_permission(current_user, channel.server, Permission(0))

    presence = getattr(request.app.state, "voice_presence", None)
    if presence is not None:
        presence.refresh(channel_id)
    return Response(status_code=204)
