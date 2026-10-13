"""Game integrations: the server owner's side of game voice."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, StringConstraints
from tortoise.functions import Count

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.GameIntegration import GameIntegration
from ..models.GameLink import GameLink
from ..models.Server import Server
from ..models.User import User
from ..permissions import Permission
from ..services.game_sessions import generate_key, hash_key, key_prefix
from ..services.permissions import permissions
from .game import LinkOut, link_out

router = APIRouter(tags=["game"])

MAX_INTEGRATIONS_PER_SERVER = 5
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
NOT_FOUND = "Integration not found"
BAD_CHANNEL = "Pick a voice channel of this server"
CHANNEL_TAKEN = "Another game already uses this voice channel"


class IntegrationOut(BaseModel):
    id: int
    server_id: int
    name: str
    voice_channel_id: int | None
    players_hear_spectators: bool
    key_prefix: str
    created_at: datetime
    linked_count: int
    active: bool


class IntegrationCreated(IntegrationOut):
    key: str


class IntegrationCreate(BaseModel):
    name: Name
    voice_channel_id: int
    players_hear_spectators: bool = False


class IntegrationUpdate(BaseModel):
    name: Name | None = None
    voice_channel_id: int | None = None
    players_hear_spectators: bool | None = None


class RotatedKey(BaseModel):
    key: str
    key_prefix: str


def integration_out(
    request: Request, integration: GameIntegration, linked_count: int
) -> IntegrationOut:
    return IntegrationOut(
        id=integration.id,
        server_id=integration.server_id,
        name=integration.name,
        voice_channel_id=integration.voice_channel_id,
        players_hear_spectators=integration.players_hear_spectators,
        key_prefix=integration.key_prefix,
        created_at=integration.created_at,
        linked_count=linked_count,
        active=request.app.state.game_sessions.is_active(integration.id),
    )


async def linked_counts(integration_ids: list[int]) -> dict[int, int]:
    rows = await (
        GameLink.filter(integration_id__in=integration_ids)
        .annotate(total=Count("id")).group_by("integration_id")
        .values("integration_id", "total"))
    return {row["integration_id"]: row["total"] for row in rows}


async def can_manage(user: User, server: Server) -> bool:
    mask = await permissions.effective(user.id, server)
    return mask is not None and bool(mask & Permission.MANAGE_SERVER)


async def managed_server(server_id: int, user: User = Depends(get_current_user)) -> Server:
    server = await Server.get_or_none(id=server_id)
    if server is None or not await can_manage(user, server):
        raise HTTPException(status_code=404, detail="Server not found")
    return server


async def managed_integration(
    integration_id: int, user: User = Depends(get_current_user)
) -> GameIntegration:
    integration = await GameIntegration.get_or_none(id=integration_id)
    server = await Server.get_or_none(id=integration.server_id) if integration else None
    if server is None or not await can_manage(user, server):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return integration


async def voice_channel_of(server: Server | int, channel_id: int) -> Channel:
    server_id = server if isinstance(server, int) else server.id
    channel = await Channel.get_or_none(id=channel_id, server_id=server_id, type="voice")
    if channel is None:
        raise HTTPException(status_code=400, detail=BAD_CHANNEL)
    return channel


async def ensure_channel_free(channel: Channel, *, except_id: int | None = None) -> None:
    taken = GameIntegration.filter(voice_channel_id=channel.id)
    if except_id is not None:
        taken = taken.exclude(id=except_id)
    if await taken.exists():
        raise HTTPException(status_code=400, detail=CHANNEL_TAKEN)


@router.get("/api/servers/{server_id}/game-integrations", response_model=list[IntegrationOut])
async def list_integrations(
    request: Request, server: Server = Depends(managed_server),
) -> list[IntegrationOut]:
    integrations = await GameIntegration.filter(server_id=server.id).order_by("id")
    counts = await linked_counts([i.id for i in integrations])
    return [integration_out(request, i, counts.get(i.id, 0)) for i in integrations]


@router.post(
    "/api/servers/{server_id}/game-integrations", status_code=201,
    response_model=IntegrationCreated)
async def create_integration(
    request: Request,
    body: IntegrationCreate,
    server: Server = Depends(managed_server),
    current_user: User = Depends(get_current_user),
) -> IntegrationCreated:
    channel = await voice_channel_of(server, body.voice_channel_id)
    await ensure_channel_free(channel)
    if await GameIntegration.filter(server_id=server.id).count() >= MAX_INTEGRATIONS_PER_SERVER:
        raise HTTPException(
            status_code=400,
            detail=f"A server can have at most {MAX_INTEGRATIONS_PER_SERVER} game integrations")
    key = generate_key()
    integration = await GameIntegration.create(
        server_id=server.id, name=body.name, voice_channel_id=channel.id,
        players_hear_spectators=body.players_hear_spectators,
        key_hash=hash_key(key), key_prefix=key_prefix(key), created_by_id=current_user.id)
    return IntegrationCreated(**integration_out(request, integration, 0).model_dump(), key=key)


@router.patch("/api/game-integrations/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    request: Request,
    body: IntegrationUpdate,
    integration: GameIntegration = Depends(managed_integration),
) -> IntegrationOut:
    sessions = request.app.state.game_sessions
    channel_changed = False
    if "voice_channel_id" in body.model_fields_set:
        if body.voice_channel_id is None:
            raise HTTPException(status_code=400, detail=BAD_CHANNEL)
        channel = await voice_channel_of(integration.server_id, body.voice_channel_id)
        await ensure_channel_free(channel, except_id=integration.id)
        channel_changed = channel.id != integration.voice_channel_id
        integration.voice_channel_id = channel.id
    if body.name is not None:
        integration.name = body.name
    if body.players_hear_spectators is not None:
        integration.players_hear_spectators = body.players_hear_spectators
    await integration.save()

    if channel_changed:
        await sessions.end(integration.id)
    else:
        await sessions.settings_changed(integration)
    counts = await linked_counts([integration.id])
    return integration_out(request, integration, counts.get(integration.id, 0))


@router.post("/api/game-integrations/{integration_id}/key", response_model=RotatedKey)
async def rotate_key(
    request: Request,
    integration: GameIntegration = Depends(managed_integration),
) -> RotatedKey:
    key = generate_key()
    integration.key_hash = hash_key(key)
    integration.key_prefix = key_prefix(key)
    await integration.save(update_fields=["key_hash", "key_prefix"])
    await request.app.state.game_sessions.end(integration.id)
    return RotatedKey(key=key, key_prefix=integration.key_prefix)


@router.delete("/api/game-integrations/{integration_id}", status_code=204)
async def delete_integration(
    request: Request,
    integration: GameIntegration = Depends(managed_integration),
) -> Response:
    sessions = request.app.state.game_sessions
    await sessions.end(integration.id)
    await integration.delete()
    sessions.links_changed(integration.id)
    request.app.state.link_codes.forget_integration(integration.id)
    return Response(status_code=204)


@router.get("/api/game-integrations/{integration_id}/links", response_model=list[LinkOut])
async def list_links(
    integration: GameIntegration = Depends(managed_integration),
) -> list[LinkOut]:
    links = await GameLink.filter(integration_id=integration.id).order_by("id").prefetch_related("user")
    return [link_out(link, link.user.username) for link in links]


@router.delete("/api/game-integrations/{integration_id}/links/{user_id}", status_code=204)
async def delete_link(
    request: Request,
    integration_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Managers can unlink anyone; a player can unlink themselves."""
    integration = await GameIntegration.get_or_none(id=integration_id)
    server = await Server.get_or_none(id=integration.server_id) if integration else None
    link = await GameLink.get_or_none(integration_id=integration_id, user_id=user_id)
    manages = server is not None and await can_manage(current_user, server)
    if integration is None or not (manages or (link is not None and user_id == current_user.id)):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    await link.delete()
    request.app.state.game_sessions.links_changed(integration.id)
    return Response(status_code=204)
