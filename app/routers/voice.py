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
from ..models.UserToServer import UserToServer
from ..permissions import Permission, require_channel, require_permission
from ..services.permissions import permissions
from ..services.voice_moderation import (
    SERVER_MUTED_ATTRIBUTE,
    publish_sources,
    server_muted_attributes,
)
from ..services.voice_presence import remove_from_voice, voice_channels_of

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
    request: Request,
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
    # can't connect. SPEAK and STREAM are enforced here, at join time, by
    # limiting which sources the token may publish.
    effective = await require_channel(current_user, channel, Permission.CONNECT)

    # One voice session per user: LiveKit only replaces a duplicate identity
    # within a room, so leave any other channel here.
    presence = getattr(request.app.state, "voice_presence", None)
    if presence is not None:
        other_channels = [c for c in presence.channels_of(current_user.id) if c != channel.id]
        await remove_from_voice(presence, other_channels, current_user.id)

    moderation = getattr(request.app.state, "voice_moderation", None)
    server_muted = moderation is not None and moderation.is_muted(
        channel.server_id, current_user.id)
    sources = publish_sources(effective, server_muted)

    room = f"channel_{channel.id}"

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        # An empty source list means "any source" to LiveKit, so no sources
        # has to be can_publish=False.
        can_publish=bool(sources),
        can_publish_sources=sources,
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
    )
    if server_muted:
        token = token.with_attributes({SERVER_MUTED_ATTRIBUTE: "true"})
    token = token.to_jwt()

    return VoiceTokenResponse(token=token, url=LIVEKIT_URL, room=room)


class VoicePresenceParticipant(BaseModel):
    user_id: int
    muted: bool
    deafened: bool
    server_muted: bool


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

    view = await permissions.view(current_user.id, server)
    channel_ids = [
        channel_id for channel_id in await Channel.filter(
            server_id=server_id).values_list("id", flat=True)
        if view.can_view(channel_id)]
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
    await require_channel(current_user, channel, not_found="Voice channel not found")

    presence = getattr(request.app.state, "voice_presence", None)
    if presence is not None:
        presence.refresh(channel_id)
    return Response(status_code=204)


async def _check_moderation(
    current_user: User, server_id: int, target_id: int, perm: Permission, *, own_action: str
) -> Server:
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_permission(current_user, server, perm, hide=True)

    if target_id == current_user.id:
        raise HTTPException(status_code=400, detail=own_action)
    if target_id == server.owner_id:
        raise HTTPException(status_code=403, detail="The owner can't be moderated")
    if current_user.id != server.owner_id:
        target_mask = await permissions.effective(target_id, server)
        if target_mask is not None and perm in target_mask:
            raise HTTPException(
                status_code=403, detail="Only the owner can moderate members with this permission")
    if not await UserToServer.filter(user_id=target_id, server_id=server.id).exists():
        raise HTTPException(status_code=404, detail="Member not found")
    return server


@router.put("/servers/{server_id}/members/{user_id}/server-mute", status_code=204)
async def server_mute_member(
    server_id: int,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Take a member's microphone away in this server's voice channels until unmuted."""
    server = await _check_moderation(
        current_user, server_id, user_id, Permission.MUTE_MEMBERS, own_action="Use your own mute")
    await _set_server_mute(request, server, user_id, True)
    return Response(status_code=204)


@router.delete("/servers/{server_id}/members/{user_id}/server-mute", status_code=204)
async def server_unmute_member(
    server_id: int,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    server = await _check_moderation(
        current_user, server_id, user_id, Permission.MUTE_MEMBERS, own_action="Use your own mute")
    await _set_server_mute(request, server, user_id, False)
    return Response(status_code=204)


async def _set_server_mute(request: Request, server: Server, user_id: int, muted: bool) -> None:
    moderation = request.app.state.voice_moderation
    moderation.set_muted(server.id, user_id, muted)

    presence = getattr(request.app.state, "voice_presence", None)
    channel_ids = await voice_channels_of(presence, server.id, user_id)
    if not channel_ids:
        return
    masks = await permissions.channel_masks(user_id, server) or {}
    for channel_id in channel_ids:
        sources = publish_sources(masks.get(channel_id, Permission(0)), muted)
        await presence.update_participant(
            channel_id, user_id, sources, server_muted_attributes(muted))


@router.post("/servers/{server_id}/members/{user_id}/disconnect", status_code=204)
async def disconnect_member(
    server_id: int,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Pull a member out of this server's voice channels. They can rejoin."""
    server = await _check_moderation(
        current_user, server_id, user_id, Permission.MOVE_MEMBERS,
        own_action="Leave voice instead")
    presence = getattr(request.app.state, "voice_presence", None)
    await remove_from_voice(
        presence, await voice_channels_of(presence, server.id, user_id), user_id)
    return Response(status_code=204)
