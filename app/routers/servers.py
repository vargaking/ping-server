from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from pydantic import BaseModel, field_validator
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..permissions import Permission, check_permission, require_permission, server_from_path
from ..models.Channel import Channel
from ..models.PermissionOverwrite import PermissionOverwrite
from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.ServerImport import ServerImport
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..services import channel_layout
from ..services.imports import storage as import_storage
from ..services.permissions import member_views, permissions
from ..services.channel_visibility import (
    announce_visibility,
    filter_order,
    filter_settings,
    send_per_member,
)
from ..services.roles import check_can_assign, seed_server_roles, standing_of
from ..services.server_icon import clean_icon_text, clean_icon_tone
from ..services.server_name import clean_server_name
from ..services.storage import ImageValidationError, storage_service
from ..services.voice_presence import close_voice_channels, remove_from_voice
from ..settings import server_creation_mode
from ..utils import require_owner
from .users import UserResponse

router = APIRouter(prefix="/servers", tags=["servers"])

SERVER_NAME_MAX = 100
WELCOME_MESSAGE_MAX = 1000


def _clean_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("Server name can't be empty")
    if len(value) > SERVER_NAME_MAX:
        raise ValueError(f"Server name must be at most {SERVER_NAME_MAX} characters")
    return value


class ServerCreate(BaseModel):
    name: str

    _name = field_validator("name")(clean_server_name)


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    server_profile: Optional[dict] = None
    server_settings: Optional[dict] = None
    icon_text: Optional[str] = None
    icon_tone: Optional[int] = None

    _name = field_validator("name")(clean_server_name)
    _icon_text = field_validator("icon_text")(clean_icon_text)
    _icon_tone = field_validator("icon_tone")(clean_icon_tone)


class ServerPublicResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    server_profile: dict
    icon_text: Optional[str] = None
    icon_tone: Optional[int] = None

    @classmethod
    def from_server(cls, server: Server):
        return cls(
            id=server.id,
            name=server.name,
            created_at=server.created_at,
            server_profile=server.server_profile,
            icon_text=server.icon_text,
            icon_tone=server.icon_tone,
        )


class ServerResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    server_profile: dict
    server_settings: dict
    icon_text: Optional[str] = None
    icon_tone: Optional[int] = None
    owner_id: Optional[int] = None
    members: List[UserResponse] = []
    # The caller's effective permission mask as a decimal string (JS numbers
    # lose precision past 2^53); filled in by the handlers.
    permissions: str = "0"
    # The caller's mask in channels where it isn't simply the server mask.
    channel_permissions: dict[str, str] = {}

    @classmethod
    def from_server(cls, server: Server):
        members = []
        for relation in server.server_users:
            if relation.user:
                members.append(UserResponse.from_user(relation.user))

        return cls(
            id=server.id,
            name=server.name,
            created_at=server.created_at,
            server_profile=server.server_profile,
            server_settings=server.server_settings,
            icon_text=server.icon_text,
            icon_tone=server.icon_tone,
            owner_id=server.owner_id,
            members=members
        )


class MemberResponse(BaseModel):
    user: UserResponse
    joined_at: datetime
    is_owner: bool
    role_ids: List[int] = []


class MemberRolesUpdate(BaseModel):
    role_ids: List[int]


def _with_channel_order(settings: dict, order: list[int]) -> dict:
    return {**(settings or {}), "channel_order": order}


async def server_response(
    server: Server, user: User, channel_order: Optional[list[int]] = None
) -> ServerResponse:
    if channel_order is None:
        channel_order = (await channel_layout.legacy_channel_orders([server.id]))[server.id]
    view = await permissions.view(user.id, server)
    response = ServerResponse.from_server(server)
    response.server_settings = _with_channel_order(
        filter_settings(response.server_settings, view), filter_order(channel_order, view))
    response.permissions = str(int(view.mask if view else 0))
    response.channel_permissions = view.differing_masks() if view else {}
    return response


async def announce_server_added(request: Request, server: Server, user: User) -> None:
    """Tell all of *user*'s sockets about a server they now belong to, so tabs
    other than the one that acted pick it up without a reload."""
    comms = getattr(request.app.state, "comms", None)
    if comms is None:
        return
    await server.fetch_related("server_users__user")
    added = await server_response(server, user)
    await comms.send_to_user(user.id, {
        "type": "server_added",
        "server": added.model_dump(mode="json"),
    })


async def create_server_for(owner: User, **fields) -> Server:
    """Create a server owned by *owner*, with the owner as its first member.
    Call inside a transaction."""
    server = await Server.create(**fields, owner=owner)
    await UserToServer.create(user=owner, server=server)
    await seed_server_roles(server)
    await channel_layout.seed_default_groups(server)
    return server


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    server: ServerCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if server_creation_mode() == "waitlist" and not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Server creation requires approval. Submit a request instead.")
    async with in_transaction():
        server_obj = await create_server_for(current_user, **server.model_dump())
    await announce_server_added(request, server_obj, current_user)
    await server_obj.fetch_related('server_users__user')
    return await server_response(server_obj, current_user)


@router.get("/", response_model=List[ServerPublicResponse])
async def get_servers(current_user: User = Depends(get_current_user)):
    # Only surface servers the caller actually belongs to — listing every
    # server on the instance leaks the existence of private servers.
    relations = await UserToServer.filter(
        user=current_user).prefetch_related("server")
    return [ServerPublicResponse.from_server(rel.server) for rel in relations]


@router.get("/me", response_model=List[ServerResponse])
async def get_my_servers(current_user: User = Depends(get_current_user)):
    # Return servers the current user belongs to
    user_server_relations = await UserToServer.filter(user=current_user).prefetch_related("server__server_users__user")
    servers = [relation.server for relation in user_server_relations]
    orders = await channel_layout.legacy_channel_orders([server.id for server in servers])
    return [
        await server_response(server, current_user, orders[server.id])
        for server in servers
    ]


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server: Server = Depends(check_permission(Permission(0), hide=True)),
    current_user: User = Depends(get_current_user),
):
    await server.fetch_related('server_users__user')
    return await server_response(server, current_user)


async def _broadcast_server_updated(request: Request, server: Server, actor_id: int) -> None:
    """Let the other members patch the server in place."""
    comms = getattr(request.app.state, "comms", None)
    if comms is None:
        return
    payload = ServerPublicResponse.from_server(server).model_dump(mode="json")
    order = (await channel_layout.legacy_channel_orders([server.id]))[server.id]
    await send_per_member(comms, server, lambda user_id, view: None if user_id == actor_id else {
        "type": "server_updated",
        "server": {**payload, "server_settings": _with_channel_order(
            filter_settings(server.server_settings, view), filter_order(order, view))},
    })


def _is_channel_reorder(update_data: dict) -> bool:
    settings = update_data.get("server_settings")
    return (
        set(update_data) == {"server_settings"}
        and isinstance(settings, dict)
        and set(settings) == {"channel_order"}
    )


def _merge_json(current: Optional[dict], incoming: dict) -> dict:
    """Incoming keys overwrite, absent keys stay, null removes."""
    merged = dict(current or {})
    for key, value in incoming.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _clean_profile_update(profile: dict) -> dict:
    # The icon has its own upload and delete endpoints.
    profile = {key: value for key, value in profile.items() if key != "icon"}
    message = profile.get("welcome_message")
    if message is not None:
        if not isinstance(message, str):
            raise HTTPException(status_code=422, detail="Welcome message must be text")
        message = message.strip()
        if len(message) > WELCOME_MESSAGE_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"Welcome message must be at most {WELCOME_MESSAGE_MAX} characters",
            )
        profile["welcome_message"] = message or None
    return profile


async def _check_default_channel(server: Server, channel_id) -> None:
    if channel_id is None:
        return
    is_text_channel = (
        isinstance(channel_id, int)
        and not isinstance(channel_id, bool)
        and await Channel.filter(id=channel_id, server_id=server.id, type="text").exists()
    )
    if not is_text_channel:
        raise HTTPException(
            status_code=422, detail="Default channel must be a text channel in this server")


@router.put("/{server_id}", response_model=ServerPublicResponse)
async def update_server(
    server_update: ServerUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    update_data = server_update.model_dump(exclude_unset=True)
    # Older clients still send channel_order when dragging channels. It is
    # accepted for one release but the layout is owned by the channel endpoints.
    channel_reorder = _is_channel_reorder(update_data)
    await require_permission(
        current_user, server,
        Permission.MANAGE_CHANNELS if channel_reorder else Permission.MANAGE_SERVER)
    if channel_reorder:
        return ServerPublicResponse.from_server(server)
    if update_data.get("server_settings") is not None:
        update_data["server_settings"] = {
            key: value for key, value in update_data["server_settings"].items()
            if key != "channel_order"}

    if "name" in update_data and update_data["name"] is None:
        raise HTTPException(status_code=422, detail="Server name can't be empty")
    if update_data.get("server_profile") is not None:
        update_data["server_profile"] = _merge_json(
            server.server_profile, _clean_profile_update(update_data["server_profile"]))
    if update_data.get("server_settings") is not None:
        incoming = update_data["server_settings"]
        if "default_channel_id" in incoming:
            await _check_default_channel(server, incoming["default_channel_id"])
        update_data["server_settings"] = _merge_json(server.server_settings, incoming)
    await server.update_from_dict(update_data)
    if server.icon_text and server.icon_tone is None:
        server.icon_tone = 1
    await server.save()

    await _broadcast_server_updated(request, server, current_user.id)
    return ServerPublicResponse.from_server(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    require_owner(current_user, server)

    # Memberships go with the server, so collect who to tell first.
    member_ids = await UserToServer.filter(server_id=server.id).values_list("user_id", flat=True)
    voice_channel_ids = await Channel.filter(server_id=server.id, type="voice").values_list("id", flat=True)
    import_ids = await ServerImport.filter(server_id=server.id).values_list("id", flat=True)
    await server.delete()
    permissions.invalidate(server_id)
    if import_ids:
        await request.app.state.import_runner.stop(import_ids)
        await import_storage.remove_import_dirs(import_ids)

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_users(
            member_ids,
            {"type": "server_deleted", "server_id": server_id},
            exclude_user_id=current_user.id,
        )

    await close_voice_channels(getattr(request.app.state, "voice_presence", None), voice_channel_ids)


@router.post("/{server_id}/icon", response_model=ServerPublicResponse)
async def upload_server_icon(
    request: Request,
    file: UploadFile = File(...),
    server: Server = Depends(check_permission(Permission.MANAGE_SERVER)),
    current_user: User = Depends(get_current_user),
):
    content = await file.read()
    try:
        url = await storage_service.upload_image(
            content,
            f"servers/{server.id}/icon",
            max_size_px=512,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if not url:
        raise HTTPException(status_code=500, detail="Failed to upload file")

    server.server_profile['icon'] = url
    await server.save()
    await _broadcast_server_updated(request, server, current_user.id)
    return ServerPublicResponse.from_server(server)


@router.delete("/{server_id}/icon", response_model=ServerPublicResponse)
async def delete_server_icon(
    request: Request,
    server: Server = Depends(check_permission(Permission.MANAGE_SERVER)),
    current_user: User = Depends(get_current_user),
):
    server.server_profile.pop('icon', None)
    await server.save()
    await _broadcast_server_updated(request, server, current_user.id)
    return ServerPublicResponse.from_server(server)


async def _assigned_role_ids(server_id: int) -> dict[int, list[int]]:
    """Assigned role ids per user. The default role is implicit and never listed."""
    role_ids = await Role.filter(server_id=server_id, is_default=False).values_list("id", flat=True)
    rows = await RoleToUser.filter(role_id__in=role_ids).order_by("role_id").values_list(
        "user_id", "role_id")
    assigned: dict[int, list[int]] = {}
    for user_id, role_id in rows:
        assigned.setdefault(user_id, []).append(role_id)
    return assigned


async def _member_response(server: Server, relation: UserToServer, role_ids: list[int]) -> MemberResponse:
    return MemberResponse(
        user=UserResponse.from_user(relation.user),
        joined_at=relation.created_at,
        is_owner=relation.user_id == server.owner_id,
        role_ids=role_ids,
    )


@router.get("/{server_id}/members", response_model=List[MemberResponse])
async def list_members(
    server: Server = Depends(check_permission(Permission(0), hide=True)),
):
    relations = await UserToServer.filter(server=server).order_by("created_at", "id").prefetch_related("user")
    assigned = await _assigned_role_ids(server.id)
    members = [
        await _member_response(server, rel, assigned.get(rel.user_id, []))
        for rel in relations
    ]
    members.sort(key=lambda m: not m.is_owner)
    return members


@router.put("/{server_id}/members/{user_id}/roles", response_model=MemberResponse)
async def set_member_roles(
    user_id: int,
    body: MemberRolesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    """Replace the roles assigned to a member (the default role is implicit)."""
    actor_mask = await require_permission(current_user, server, Permission.MANAGE_ROLES)

    membership = await UserToServer.get_or_none(
        user_id=user_id, server=server).prefetch_related("user")
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")
    if user_id == server.owner_id:
        raise HTTPException(status_code=403, detail="The owner's permissions can't be changed")

    wanted = set(body.role_ids)
    roles = {role.id: role for role in await Role.filter(server=server)}
    if any(role_id not in roles or roles[role_id].is_default for role_id in wanted):
        raise HTTPException(
            status_code=422, detail="Roles must be existing, non-default roles of this server")

    current = set(await RoleToUser.filter(
        user_id=user_id, role_id__in=list(roles)).values_list("role_id", flat=True))
    standing = await standing_of(current_user, server, actor_mask, roles)
    for role_id in wanted ^ current:
        check_can_assign(standing, roles, roles[role_id])

    before = await member_views(server)
    async with in_transaction():
        await RoleToUser.filter(user_id=user_id, role_id__in=list(roles)).delete()
        await RoleToUser.bulk_create(
            [RoleToUser(user_id=user_id, role_id=role_id) for role_id in sorted(wanted)])
    permissions.invalidate(server.id)

    role_ids = sorted(wanted)
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(server.id, {
            "type": "member_roles_updated",
            "server_id": server.id,
            "user_id": user_id,
            "role_ids": role_ids,
        })
    await announce_visibility(request.app.state, server, before, await member_views(server))

    return await _member_response(server, membership, role_ids)


@router.delete("/{server_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission(0), hide=True)),
):
    """Kick a member (needs KICK_MEMBERS) or leave the server (user_id is yourself)."""
    leaving = user_id == current_user.id

    if leaving:
        if server.owner_id == current_user.id:
            raise HTTPException(
                status_code=409,
                detail="The owner can't leave. Transfer ownership or delete the server.")
    else:
        await require_permission(current_user, server, Permission.KICK_MEMBERS)
        if user_id == server.owner_id:
            raise HTTPException(status_code=403, detail="The owner can't be removed")
        if current_user.id != server.owner_id:
            target_mask = await permissions.effective(user_id, server)
            if target_mask is not None and Permission.KICK_MEMBERS in target_mask:
                raise HTTPException(
                    status_code=403,
                    detail="Only the owner can remove members who can kick")

    membership = await UserToServer.get_or_none(user_id=user_id, server=server)
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Collect everyone to tell before the membership row goes away, so the
    # removed user's own sockets hear about it too.
    member_ids = await UserToServer.filter(server=server).values_list("user_id", flat=True)
    await membership.delete()
    role_ids = await Role.filter(server=server).values_list("id", flat=True)
    await RoleToUser.filter(user_id=user_id, role_id__in=role_ids).delete()
    await PermissionOverwrite.filter(server=server, user_id=user_id).delete()
    permissions.invalidate(server.id)

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_users(member_ids, {
            "type": "member_left",
            "server_id": server.id,
            "user_id": user_id,
            "reason": "left" if leaving else "kicked",
        })

    voice_channel_ids = await Channel.filter(server=server, type="voice").values_list("id", flat=True)
    await remove_from_voice(
        getattr(request.app.state, "voice_presence", None), voice_channel_ids, user_id)
    if moderation := getattr(request.app.state, "voice_moderation", None):
        moderation.set_muted(server.id, user_id, False)
