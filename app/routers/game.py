"""Game voice, game side and player side.

A game server authenticates with an integration key and sends the audio mix of
its players. Players link their game account to their Zet account with a code.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from limits import parse
from pydantic import BaseModel, Field, ValidationError
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.GameIntegration import GameIntegration
from ..models.GameLink import GameLink
from ..models.Server import Server
from ..models.User import User
from ..permissions import Permission
from ..rate_limit import client_address, game_key, limiter, user_key
from ..services.game_sessions import (
    CODE_TTL_SECONDS,
    IntegrationChanged,
    NoVoiceChannel,
    bearer_token,
    hash_key,
)
from ..services.permissions import permissions
from ..settings import (
    app_url,
    game_auth_fail_rate_limit,
    game_frame_rate_limit,
    game_link_rate_limit,
)

router = APIRouter(prefix="/api/game", tags=["game"])

MAX_FRAME_BYTES = 512 * 1024
MAX_FRAME_PLAYERS = 200

game_limit = limiter.shared_limit(game_frame_rate_limit, scope="game_api", key_func=game_key)
link_limit = limiter.shared_limit(game_link_rate_limit, scope="game_link_codes", key_func=user_key)


class LinkOut(BaseModel):
    user_id: int
    username: str
    player_id: str
    player_name: str
    created_at: datetime


def link_out(link: GameLink, username: str) -> LinkOut:
    return LinkOut(
        user_id=link.user_id, username=username, player_id=link.player_id,
        player_name=link.player_name, created_at=link.created_at)


async def game_integration(request: Request) -> GameIntegration:
    token = bearer_token(request.headers.get("authorization"))
    integration = (
        await GameIntegration.get_or_none(key_hash=hash_key(token)) if token else None)
    if integration is None:
        # Per-key limits only see requests that authenticate, so failures are
        # counted per address here.
        failures = parse(game_auth_fail_rate_limit())
        if not limiter.limiter.hit(failures, "game_auth_fail", client_address(request)):
            raise HTTPException(
                status_code=429, detail="Too many failed attempts",
                headers={"Retry-After": str(failures.get_expiry())})
        raise HTTPException(
            status_code=401, detail="Invalid game key",
            headers={"WWW-Authenticate": "Bearer"})
    return integration


class LinkCodeCreate(BaseModel):
    player_id: str = Field(min_length=1, max_length=64)
    player_name: str = Field(min_length=1, max_length=64)


class LinkCodeCreated(BaseModel):
    code: str
    url: str
    expires_in: int


class FrameIn(BaseModel):
    players: list[str]
    mix: dict[str, dict[str, float]] = {}


class FrameResult(BaseModel):
    linked: list[str]


@router.post("/v1/link-codes", status_code=201, response_model=LinkCodeCreated)
@game_limit
async def create_link_code(
    request: Request,
    response: Response,
    body: LinkCodeCreate,
    integration: GameIntegration = Depends(game_integration),
) -> LinkCodeCreated:
    code = request.app.state.link_codes.create(
        integration.id, body.player_id, body.player_name)
    return LinkCodeCreated(
        code=code, url=f"{app_url()}/app/link-game?code={code}",
        expires_in=int(CODE_TTL_SECONDS))


@router.delete("/v1/links/{player_id}", status_code=204)
@game_limit
async def unlink_player(
    request: Request,
    response: Response,
    player_id: str,
    integration: GameIntegration = Depends(game_integration),
) -> Response:
    deleted = await GameLink.filter(
        integration_id=integration.id, player_id=player_id).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail="Not linked")
    request.app.state.game_sessions.links_changed(integration.id)
    return Response(status_code=204)


async def read_limited(request: Request, limit: int) -> bytes:
    too_large = HTTPException(status_code=413, detail="Frame too large")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/frames", response_model=FrameResult)
@game_limit
async def post_frame(
    request: Request,
    response: Response,
    integration: GameIntegration = Depends(game_integration),
) -> FrameResult:
    body = await read_limited(request, MAX_FRAME_BYTES)
    try:
        frame = FrameIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False, include_input=False))
    players = list(dict.fromkeys(frame.players))
    if len(players) > MAX_FRAME_PLAYERS:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_FRAME_PLAYERS} players per frame")

    sessions = request.app.state.game_sessions
    try:
        linked = await sessions.frame(integration, players, frame.mix)
    except NoVoiceChannel:
        raise HTTPException(status_code=409, detail="No voice channel set")
    except IntegrationChanged:
        raise HTTPException(status_code=409, detail="Integration changed, retry")
    return FrameResult(linked=linked)


@router.delete("/v1/session", status_code=204)
@game_limit
async def end_session(
    request: Request,
    response: Response,
    integration: GameIntegration = Depends(game_integration),
) -> Response:
    await request.app.state.game_sessions.end(integration.id)
    return Response(status_code=204)


class LinkCodePreview(BaseModel):
    integration_id: int
    integration_name: str
    server_id: int
    server_name: str
    player_name: str
    linked_player_name: str | None


async def resolve_code(request: Request, code: str, user: User, *, consume: bool):
    """The pending code and its integration for a member of the integration's server."""
    codes = request.app.state.link_codes
    entry = codes.get(code)
    integration = await GameIntegration.get_or_none(id=entry.integration_id) if entry else None
    if entry is None or integration is None:
        raise HTTPException(status_code=404, detail="Code not found or expired")
    server = await Server.get(id=integration.server_id)
    if await permissions.effective(user.id, server) is None:
        raise HTTPException(status_code=403, detail="Not a member of this server")
    if consume and codes.consume(code) is None:
        raise HTTPException(status_code=404, detail="Code not found or expired")
    return entry, integration, server


@router.get("/link-codes/{code}", response_model=LinkCodePreview)
@link_limit
async def preview_link_code(
    request: Request,
    response: Response,
    code: str,
    current_user: User = Depends(get_current_user),
) -> LinkCodePreview:
    entry, integration, server = await resolve_code(
        request, code, current_user, consume=False)
    existing = await GameLink.get_or_none(
        integration_id=integration.id, user_id=current_user.id)
    return LinkCodePreview(
        integration_id=integration.id,
        integration_name=integration.name,
        server_id=server.id,
        server_name=server.name,
        player_name=entry.player_name,
        linked_player_name=existing.player_name if existing else None,
    )


@router.post("/link-codes/{code}/claim", response_model=LinkOut)
@link_limit
async def claim_link_code(
    request: Request,
    response: Response,
    code: str,
    current_user: User = Depends(get_current_user),
) -> LinkOut:
    """Whoever holds the game account just proved it, so this replaces the
    user's earlier link here and any other user's link to the same player."""
    entry, integration, _ = await resolve_code(request, code, current_user, consume=True)
    async with in_transaction():
        await GameLink.filter(integration_id=integration.id).filter(
            Q(user_id=current_user.id) | Q(player_id=entry.player_id)).delete()
        link = await GameLink.create(
            integration_id=integration.id, user_id=current_user.id,
            player_id=entry.player_id, player_name=entry.player_name)
    request.app.state.game_sessions.links_changed(integration.id)
    return link_out(link, current_user.username)


@router.get("/sessions/{channel_id}")
async def get_channel_session(
    request: Request,
    channel_id: int,
    current_user: User = Depends(get_current_user),
):
    channel = await Channel.get_or_none(id=channel_id)
    mask = (
        await permissions.effective(
            current_user.id, channel.server_id, channel_id=channel.id)
        if channel else None)
    if mask is None or not mask & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=404, detail="Channel not found")
    session = request.app.state.game_sessions.session_for_channel(channel_id)
    if session is None:
        return Response(status_code=204)
    return JSONResponse(session)
