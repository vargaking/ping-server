import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

from ..middleware import get_current_user
from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.User import User
from ..permissions import ALL_PERMISSIONS, Permission, check_permission, require_permission, server_from_path
from ..services.role_resolution import descendants, would_cycle
from ..services.roles import (
    ABOVE_YOUR_RANK,
    INHERITED_ABOVE_YOU,
    MAX_ROLES,
    apply_role_change,
    check_can_edit,
    renumber_roles,
    role_positions,
    shift_roles_up,
    standing_of,
)

router = APIRouter(prefix="/servers", tags=["roles"])

ROLE_NAME_MAX = 50
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


class RoleResponse(BaseModel):
    id: int
    name: str
    allow: str
    deny: str
    parent_id: Optional[int] = None
    is_default: bool
    position: int
    color: Optional[str] = None

    @classmethod
    def from_role(cls, role: Role):
        return cls(
            id=role.id,
            name=role.name,
            allow=str(role.allow),
            deny=str(role.deny),
            parent_id=role.parent_id,
            is_default=role.is_default,
            position=role.position,
            color=role.color,
        )


class RoleFields(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    allow: Optional[int] = None
    deny: Optional[int] = None
    parent_id: Optional[StrictInt] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not 1 <= len(value) <= ROLE_NAME_MAX:
            raise ValueError(f"Role name must be 1 to {ROLE_NAME_MAX} characters")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not COLOR_PATTERN.match(value):
            raise ValueError("Color must look like #rrggbb")
        return value

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _bits(cls, value):
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            if value is None:
                return value
            raise ValueError("Permissions must be decimal strings")
        if value < 0 or value & ~int(ALL_PERMISSIONS):
            raise ValueError("Unknown permission bit")
        return value

    @model_validator(mode="after")
    def _required_when_present(self):
        for field in ("name", "allow", "deny"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} can't be null")
        if (self.allow or 0) & (self.deny or 0):
            raise ValueError("A permission can't be both allowed and denied")
        return self


class RoleCreate(RoleFields):
    name: str


class RoleUpdate(RoleFields):
    pass


class RoleOrder(BaseModel):
    role_ids: List[StrictInt] = Field(max_length=MAX_ROLES)


async def _roles_of(server_id: int) -> dict[int, Role]:
    return {role.id: role for role in await Role.filter(server_id=server_id)}


async def _listing(server_id: int) -> list[RoleResponse]:
    roles = await Role.filter(server_id=server_id).order_by("-position", "-id")
    return [RoleResponse.from_role(role) for role in roles]


def _changes(body: RoleFields, role: Role) -> dict:
    """The fields a request sets, with @everyone's restrictions applied."""
    fields = body.model_fields_set
    changes = {
        field: getattr(body, field)
        for field in ("name", "color", "allow", "deny", "parent_id") if field in fields
    }
    if role.is_default:
        if "name" in changes and changes["name"] != role.name:
            raise HTTPException(status_code=422, detail="@everyone can't be renamed")
        if changes.get("parent_id") is not None:
            raise HTTPException(status_code=422, detail="@everyone can't inherit from a role")
        if changes.get("deny"):
            raise HTTPException(status_code=422, detail="@everyone can't deny permissions")
    return changes


def _check_parent(role: Role, parent_id: Optional[int], roles: dict[int, Role]) -> None:
    if parent_id is None or parent_id == role.parent_id:
        return
    if parent_id not in roles:
        raise HTTPException(
            status_code=422, detail="The parent must be a role of this server")
    if would_cycle(role.id, parent_id, roles):
        raise HTTPException(
            status_code=422, detail=f"That would make {role.name} inherit from itself")


async def _check_name_free(server_id: int, name: str, role_id: Optional[int]) -> None:
    clash = Role.filter(server_id=server_id, name=name)
    if role_id is not None:
        clash = clash.exclude(id=role_id)
    if await clash.exists():
        raise HTTPException(status_code=409, detail="A role with that name already exists")


async def _edit(role: Role, changes: dict, roles: dict[int, Role], standing) -> Role:
    allow = changes.get("allow", role.allow)
    deny = changes.get("deny", role.deny)
    parent_id = changes.get("parent_id", role.parent_id)
    if allow & deny:
        raise HTTPException(
            status_code=422, detail="A permission can't be both allowed and denied")
    check_can_edit(standing, roles, role, allow, deny, parent_id)
    _check_parent(role, parent_id, roles)
    if "name" in changes and changes["name"] != role.name:
        await _check_name_free(role.server_id, changes["name"], role.id)
    role.update_from_dict({**changes, "allow": allow, "deny": deny, "parent_id": parent_id})
    await role.save()
    return role


async def _role_or_404(server_id: int, role_id: int, roles: dict[int, Role]) -> Role:
    role = roles.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.get("/{server_id}/roles", response_model=List[RoleResponse])
async def list_roles(
    server: Server = Depends(check_permission(Permission(0), hide=True)),
):
    return await _listing(server.id)


@router.post(
    "/{server_id}/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    """Create a role right above @everyone, shifting the others up."""
    actor_mask = await require_permission(current_user, server, Permission.MANAGE_ROLES)

    async def mutate() -> Role:
        roles = await _roles_of(server.id)
        if len(roles) >= MAX_ROLES:
            raise HTTPException(
                status_code=422, detail=f"A server can have at most {MAX_ROLES} roles")
        standing = await standing_of(current_user, server, actor_mask, roles)
        if not standing.is_owner and standing.rank < 1:
            raise HTTPException(status_code=403, detail=ABOVE_YOUR_RANK)
        await _check_name_free(server.id, body.name, None)
        await shift_roles_up(server.id)
        role = await Role.create(server=server, name=body.name, position=1)
        roles = await _roles_of(server.id)
        changes = _changes(body, role)
        await _edit(roles[role.id], changes, roles, standing.above_created_roles())
        return roles[role.id]

    async def event(role: Role) -> dict:
        return {
            "type": "role_created",
            "server_id": server.id,
            "role": RoleResponse.from_role(role).model_dump(),
            "positions": await role_positions(server.id),
        }

    return RoleResponse.from_role(await apply_role_change(request, server, mutate, event))


@router.put("/{server_id}/roles/order", response_model=List[RoleResponse])
async def reorder_roles(
    body: RoleOrder,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    """Set the order of the non-default roles, highest first."""
    actor_mask = await require_permission(current_user, server, Permission.MANAGE_ROLES)

    async def mutate() -> None:
        roles = await _roles_of(server.id)
        movable = {role_id for role_id, role in roles.items() if not role.is_default}
        if len(body.role_ids) != len(movable) or set(body.role_ids) != movable:
            raise HTTPException(
                status_code=422,
                detail="List every role except @everyone exactly once, highest first")
        standing = await standing_of(current_user, server, actor_mask, roles)
        count = len(body.role_ids)
        moves = {
            role_id: count - index for index, role_id in enumerate(body.role_ids)
            if roles[role_id].position != count - index
        }
        for role_id, position in moves.items():
            old = roles[role_id].position
            if not standing.is_owner and (old >= standing.rank or position >= standing.rank):
                raise HTTPException(status_code=403, detail=ABOVE_YOUR_RANK)
        for role_id, position in moves.items():
            await Role.filter(id=role_id).update(position=position)

    async def event(_: None) -> dict:
        return {
            "type": "roles_reordered",
            "server_id": server.id,
            "positions": await role_positions(server.id),
        }

    await apply_role_change(request, server, mutate, event)
    return await _listing(server.id)


@router.patch("/{server_id}/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    body: RoleUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    actor_mask = await require_permission(current_user, server, Permission.MANAGE_ROLES)

    async def mutate() -> Role:
        roles = await _roles_of(server.id)
        role = await _role_or_404(server.id, role_id, roles)
        standing = await standing_of(current_user, server, actor_mask, roles)
        return await _edit(role, _changes(body, role), roles, standing)

    async def event(role: Role) -> dict:
        return {
            "type": "role_updated",
            "server_id": server.id,
            "role": RoleResponse.from_role(role).model_dump(),
        }

    return RoleResponse.from_role(await apply_role_change(request, server, mutate, event))


@router.delete("/{server_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
):
    """Delete a role. Roles inheriting from it lose the parent; its assignments go."""
    actor_mask = await require_permission(current_user, server, Permission.MANAGE_ROLES)

    async def mutate() -> None:
        roles = await _roles_of(server.id)
        role = await _role_or_404(server.id, role_id, roles)
        if role.is_default:
            raise HTTPException(status_code=422, detail="@everyone can't be deleted")
        standing = await standing_of(current_user, server, actor_mask, roles)
        standing.check_touch(role)
        for descendant_id in descendants(role.id, roles):
            if not standing.can_touch(roles[descendant_id]):
                raise HTTPException(status_code=403, detail=INHERITED_ABOVE_YOU)
        await Role.filter(parent_id=role.id).update(parent_id=None)
        await RoleToUser.filter(role_id=role.id).delete()
        await role.delete()
        await renumber_roles(server.id)

    async def event(_: None) -> dict:
        return {
            "type": "role_deleted",
            "server_id": server.id,
            "role_id": role_id,
            "positions": await role_positions(server.id),
        }

    await apply_role_change(request, server, mutate, event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
