from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from pydantic import BaseModel, field_validator

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..services.storage import ImageValidationError, storage_service
from ..services.voice_presence import remove_from_voice
from ..utils import require_owner
from .users import UserResponse

router = APIRouter(prefix="/servers", tags=["servers"])

SERVER_NAME_MAX = 100


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
    server_profile: dict = {}
    server_settings: dict = {}

    _name = field_validator("name")(_clean_name)


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    server_profile: Optional[dict] = None
    server_settings: Optional[dict] = None

    _name = field_validator("name")(_clean_name)

class ServerPublicResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    server_profile: dict

    @classmethod
    def from_server(cls, server: Server):
        return cls(
            id=server.id,
            name=server.name,
            created_at=server.created_at,
            server_profile=server.server_profile,
        )


class ServerResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    server_profile: dict
    server_settings: dict
    owner_id: Optional[int] = None
    members: List[UserResponse] = []

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
            owner_id=server.owner_id,
            members=members
        )


class MemberResponse(BaseModel):
    user: UserResponse
    joined_at: datetime
    is_owner: bool


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(server: ServerCreate, current_user: User = Depends(get_current_user)):
    server_obj = await Server.create(**server.model_dump(), owner=current_user)
    # Automatically add the creator as a member
    await UserToServer.create(user=current_user, server=server_obj)
    await server_obj.fetch_related('server_users__user')
    return ServerResponse.from_server(server_obj)


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
    return [ServerResponse.from_server(server) for server in servers]


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(server_id: int, current_user: User = Depends(get_current_user)):
    server = await Server.get_or_none(id=server_id).prefetch_related('server_users__user')
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Non-members get a 404, not a 403: a 403 would confirm the server exists.
    is_member = await UserToServer.filter(
        user=current_user, server=server).exists()
    if not is_member:
        raise HTTPException(status_code=404, detail="Server not found")

    return ServerResponse.from_server(server)


async def _broadcast_server_updated(request: Request, server: Server, actor_id: int) -> None:
    """Let the other members patch the server's name/profile in place."""
    comms = getattr(request.app.state, "comms", None)
    if comms is None:
        return
    await comms.broadcast_to_server(server.id, {
        "type": "server_updated",
        "server": ServerPublicResponse.from_server(server).model_dump(mode="json"),
    }, exclude_user_id=actor_id)


@router.put("/{server_id}", response_model=ServerPublicResponse)
async def update_server(
    server_id: int,
    server_update: ServerUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    require_owner(current_user, server)

    update_data = server_update.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"] is None:
        raise HTTPException(status_code=422, detail="Server name can't be empty")
    await server.update_from_dict(update_data)
    await server.save()

    # Channel reordering also goes through here; only name and profile changes
    # are visible to other members.
    if "name" in update_data or "server_profile" in update_data:
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
    await server.delete()

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_users(
            member_ids,
            {"type": "server_deleted", "server_id": server_id},
            exclude_user_id=current_user.id,
        )


@router.post("/{server_id}/icon", response_model=ServerPublicResponse)
async def upload_server_icon(
    server_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    require_owner(current_user, server)

    content = await file.read()
    try:
        url = await storage_service.upload_image(
            content,
            f"servers/{server_id}/icon",
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


async def _get_member_server(server_id: int, user: User) -> Server:
    server = await Server.get_or_none(id=server_id)
    # Non-members get a 404, not a 403: a 403 would confirm the server exists.
    if not server or not await UserToServer.filter(user=user, server=server).exists():
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.get("/{server_id}/members", response_model=List[MemberResponse])
async def list_members(server_id: int, current_user: User = Depends(get_current_user)):
    server = await _get_member_server(server_id, current_user)
    relations = await UserToServer.filter(server=server).order_by("created_at", "id").prefetch_related("user")
    members = [
        MemberResponse(
            user=UserResponse.from_user(rel.user),
            joined_at=rel.created_at,
            is_owner=rel.user_id == server.owner_id,
        )
        for rel in relations
    ]
    members.sort(key=lambda m: not m.is_owner)
    return members


@router.delete("/{server_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    server_id: int,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Kick a member (owner only) or leave the server (user_id is yourself)."""
    server = await _get_member_server(server_id, current_user)
    leaving = user_id == current_user.id

    if leaving:
        if server.owner_id == current_user.id:
            raise HTTPException(
                status_code=409,
                detail="The owner can't leave. Transfer ownership or delete the server.")
    else:
        require_owner(current_user, server)

    membership = await UserToServer.get_or_none(user_id=user_id, server=server)
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Collect everyone to tell before the membership row goes away, so the
    # removed user's own sockets hear about it too.
    member_ids = await UserToServer.filter(server=server).values_list("user_id", flat=True)
    await membership.delete()
    role_ids = await Role.filter(server=server).values_list("id", flat=True)
    await RoleToUser.filter(user_id=user_id, role_id__in=role_ids).delete()

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
