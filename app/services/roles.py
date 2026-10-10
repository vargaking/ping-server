from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Mapping, TypeVar

from fastapi import HTTPException, Request
from tortoise.expressions import F

from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.User import User
from ..permissions import (
    ADMIN_PERMISSIONS,
    ADMIN_ROLE_NAME,
    ALL_PERMISSIONS,
    DEFAULT_ROLE_NAME,
    MEMBER_PERMISSIONS,
    Permission,
)
from . import channel_layout
from .channel_visibility import visibility_change
from .role_resolution import RoleLike, descendants, resolved

MAX_ROLES = 100

ABOVE_YOUR_RANK = "That role is at or above your highest role"
BITS_YOU_LACK = "You can't change permissions you don't have"
INHERITED_ABOVE_YOU = "A role above yours inherits from this role"

T = TypeVar("T")


async def seed_server_roles(server: Server) -> None:
    """Give a new server its default @everyone role and the Admin role."""
    await Role.create(
        server=server, name=DEFAULT_ROLE_NAME, is_default=True, position=0,
        allow=int(MEMBER_PERMISSIONS))
    await Role.create(
        server=server, name=ADMIN_ROLE_NAME, position=1, allow=int(ADMIN_PERMISSIONS))


@dataclass(frozen=True)
class Standing:
    """What an actor may do to roles: their mask and the position of their
    highest role. The owner skips every rule."""
    mask: int
    rank: int
    is_owner: bool
    assigned: frozenset[int] = frozenset()

    def can_touch(self, role: RoleLike) -> bool:
        return self.is_owner or role.position < self.rank

    def check_touch(self, role: RoleLike) -> None:
        if not self.can_touch(role):
            raise HTTPException(status_code=403, detail=ABOVE_YOUR_RANK)

    def above_created_roles(self) -> "Standing":
        """The standing once a new role has been inserted at position 1."""
        return replace(self, rank=self.rank + 1)


async def standing_of(
    user: User, server: Server, mask: Permission, roles: Mapping[int, RoleLike]
) -> Standing:
    if user.id == server.owner_id:
        return Standing(mask=int(ALL_PERMISSIONS), rank=0, is_owner=True)
    assigned = await RoleToUser.filter(
        user_id=user.id, role_id__in=list(roles)).values_list("role_id", flat=True)
    rank = max((roles[role_id].position for role_id in assigned), default=0)
    return Standing(mask=int(mask), rank=rank, is_owner=False, assigned=frozenset(assigned))


class _Edited:
    """A role as it would be after an edit."""

    def __init__(self, role: RoleLike, allow: int, deny: int, parent_id: int | None) -> None:
        self.id = role.id
        self.allow = allow
        self.deny = deny
        self.parent_id = parent_id
        self.position = role.position
        self.is_default = role.is_default


def check_can_edit(
    standing: Standing, roles: Mapping[int, RoleLike], role: RoleLike,
    allow: int, deny: int, parent_id: int | None,
) -> None:
    """The rank and bit rules for editing *role*, whose allow, deny and parent
    become the given values."""
    if standing.is_owner:
        return
    standing.check_touch(role)
    for descendant_id in descendants(role.id, roles):
        if not standing.can_touch(roles[descendant_id]):
            raise HTTPException(status_code=403, detail=INHERITED_ABOVE_YOU)
    changed = (role.allow ^ allow) | (role.deny ^ deny)
    after = {**roles, role.id: _Edited(role, allow, deny, parent_id)}
    if changed & ~standing.mask or resolved(role.id, after)[0] & ~standing.mask:
        raise HTTPException(status_code=403, detail=BITS_YOU_LACK)


def check_can_assign(standing: Standing, roles: Mapping[int, RoleLike], role: RoleLike) -> None:
    if standing.is_owner:
        return
    standing.check_touch(role)
    if resolved(role.id, roles)[0] & ~standing.mask:
        raise HTTPException(status_code=403, detail=BITS_YOU_LACK)


async def role_positions(server_id: int) -> list[dict]:
    rows = await Role.filter(server_id=server_id).order_by("-position", "-id").values(
        "id", "position")
    return [{"id": row["id"], "position": row["position"]} for row in rows]


async def shift_roles_up(server_id: int) -> None:
    await Role.filter(server_id=server_id, is_default=False).update(position=F("position") + 1)


async def renumber_roles(server_id: int) -> None:
    """Make the non-default positions dense again, keeping their order."""
    roles = await Role.filter(server_id=server_id, is_default=False).order_by("position", "id")
    for position, role in enumerate(roles, start=1):
        if role.position != position:
            await Role.filter(id=role.id).update(position=position)


async def apply_role_change(
    request: Request, server: Server,
    mutate: Callable[[], Awaitable[T]],
    event_builder: Callable[[T], Awaitable[dict]],
) -> T:
    """Run a role change under the server lock, then refresh permissions,
    tell the members what it changed for them and broadcast the role event."""
    async with visibility_change(request.app.state, server):
        async with channel_layout.locked_server(server.id):
            result = await mutate()
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(server.id, await event_builder(result))
    return result
