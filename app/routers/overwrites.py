from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator, model_validator

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.PermissionOverwrite import PermissionOverwrite
from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..permissions import Permission, require_channel, require_permission
from ..services import channel_layout
from ..services.overwrite_fanout import OverwriteChange, announce_overwrite, row_json
from ..services.permissions import (
    NO_ROW,
    OverwriteRows,
    RowKey,
    ServerRules,
    load_rows,
    permissions,
)
from ..services.role_resolution import CHANNEL_BITS, Bits, member_mask
from ..services.roles import BITS_YOU_LACK, Standing, standing_of
from ..services.server_queue import server_queue

router = APIRouter(tags=["permission overwrites"])

LOSE_OWN_ACCESS = "You would lose access to this channel"
MEMBER_ABOVE_YOU = "That member's highest role is at or above yours"
SUBJECTS = {"roles": "role", "members": "member"}


class OverwriteBody(BaseModel):
    allow: int = 0
    deny: int = 0

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _bits(cls, value):
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Permissions must be decimal strings")
        if value < 0 or value & ~CHANNEL_BITS:
            raise ValueError("Only channel permissions can be overwritten")
        return value

    @model_validator(mode="after")
    def _disjoint(self):
        if self.allow & self.deny:
            raise ValueError("A permission can't be both allowed and denied")
        return self


@dataclass
class Target:
    """A channel or a category, with its server."""
    server: Server
    channel: Channel | None = None
    group: ChannelGroup | None = None

    @property
    def filters(self) -> dict:
        if self.channel is not None:
            return {"channel_id": self.channel.id}
        return {"group_id": self.group.id}

    def key(self, kind: Literal["roles", "members"], subject_id: int) -> RowKey:
        if self.channel is not None:
            return RowKey("channel", self.channel.id, SUBJECTS[kind], subject_id)
        return RowKey("group", self.group.id, SUBJECTS[kind], subject_id)

    def rules(self, roles: Mapping[int, Role], rows: OverwriteRows) -> ServerRules:
        """The rules as far as masks on this target need them."""
        channels = {} if self.channel is None else {self.channel.id: self.channel.group_id}
        return ServerRules(self.server.owner_id, roles, channels, [], rows)


async def _channel_target(channel_id: int, user: User) -> Target:
    channel = await Channel.filter(id=channel_id).select_related("server").first()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    await require_channel(user, channel, server=channel.server)
    return Target(channel.server, channel=channel)


async def _group_target(group_id: int, user: User) -> Target:
    group = await ChannelGroup.filter(id=group_id).select_related("server").first()
    if group is None:
        raise HTTPException(status_code=404, detail="Category not found")
    server = group.server
    await require_permission(user, server, Permission(0), hide=True)
    view = await permissions.view(user.id, server)
    if group.id not in view.groups:
        raise HTTPException(status_code=404, detail="Category not found")
    return Target(server, group=group)


def _row_json(row: PermissionOverwrite) -> dict:
    subject = {"role_id": row.role_id} if row.role_id is not None else {"user_id": row.user_id}
    return {**subject, "allow": str(row.allow), "deny": str(row.deny)}


async def _listing(target: Target, user: User) -> dict:
    await require_permission(user, target.server, Permission.MANAGE_ROLES)
    rows = await PermissionOverwrite.filter(**target.filters).order_by("id")
    return {
        "roles": [_row_json(row) for row in rows if row.role_id is not None],
        "members": [_row_json(row) for row in rows if row.user_id is not None],
    }


async def _check_subject(
    target: Target, standing: Standing, roles: dict[int, Role],
    kind: Literal["roles", "members"], subject_id: int,
) -> None:
    server = target.server
    if kind == "roles":
        role = roles.get(subject_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Role not found")
        standing.check_touch(role)
        return
    if not await UserToServer.filter(user_id=subject_id, server_id=server.id).exists():
        raise HTTPException(status_code=404, detail="Member not found")
    if standing.is_owner:
        return
    if subject_id == server.owner_id:
        raise HTTPException(status_code=403, detail=MEMBER_ABOVE_YOU)
    assigned = await RoleToUser.filter(
        user_id=subject_id, role_id__in=list(roles)).values_list("role_id", flat=True)
    rank = max((roles[role_id].position for role_id in assigned), default=0)
    if rank >= standing.rank and rank > 0:
        raise HTTPException(status_code=403, detail=MEMBER_ABOVE_YOU)


async def _authorize(
    target: Target, user: User, actor_mask: Permission,
    kind: Literal["roles", "members"], subject_id: int,
) -> tuple[dict[int, Role], Standing]:
    """The rank and subject rules (404s and 403s)."""
    roles = {role.id: role for role in await Role.filter(server_id=target.server.id)}
    standing = await standing_of(user, target.server, actor_mask, roles)
    await _check_subject(target, standing, roles, kind, subject_id)
    return roles, standing


def _check_bits(
    target: Target, user: User, roles: Mapping[int, Role], standing: Standing,
    rows: OverwriteRows, key: RowKey, bits: Bits,
) -> None:
    """A non-owner may only change bits they hold there, and may not lose View."""
    rules = target.rules(roles, rows)
    base = member_mask(roles, standing.assigned)
    old = rows.bits(key)
    changed = (old[0] ^ bits[0]) | (old[1] ^ bits[1])
    if changed & ~int(rules.target_mask(user.id, base, standing.assigned, key)):
        raise HTTPException(status_code=403, detail=BITS_YOU_LACK)
    after = rules.with_row(key, bits).target_mask(user.id, base, standing.assigned, key)
    if not after & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=403, detail=LOSE_OWN_ACCESS)


async def _save(server_id: int, key: RowKey, old: Bits, bits: Bits) -> None:
    rows = PermissionOverwrite.filter(**key.filters)
    if bits == NO_ROW:
        await rows.delete()
    elif old == NO_ROW:
        await PermissionOverwrite.create(
            server_id=server_id, **key.filters, allow=bits[0], deny=bits[1])
    else:
        await rows.update(allow=bits[0], deny=bits[1])


async def _write(
    request: Request, target: Target, user: User,
    kind: Literal["roles", "members"], subject_id: int, body: OverwriteBody,
) -> dict | None:
    """Set one overwrite row (or delete it when both masks are empty) under the
    rules for who may edit. What it changed is announced after the response."""
    server = target.server
    actor_mask = await require_permission(user, server, Permission.MANAGE_ROLES)
    key = target.key(kind, subject_id)
    bits = (body.allow, body.deny)

    saved = await PermissionOverwrite.get_or_none(**key.filters)
    if bits == (NO_ROW if saved is None else (saved.allow, saved.deny)):
        roles, standing = await _authorize(target, user, actor_mask, kind, subject_id)
        if not standing.is_owner:
            _check_bits(target, user, roles, standing, await load_rows(server.id), key, bits)
        return row_json(key, bits)

    slot = None
    try:
        async with channel_layout.locked_server(server.id):
            rows = await load_rows(server.id)
            roles, standing = await _authorize(target, user, actor_mask, kind, subject_id)
            if not standing.is_owner:
                _check_bits(target, user, roles, standing, rows, key, bits)
            if rows.bits(key) == bits:
                return row_json(key, bits)
            await _save(server.id, key, rows.bits(key), bits)
            # Reserved under the lock so jobs run in commit order.
            slot = server_queue.reserve(server.id)
    except BaseException:
        if slot is not None:
            # The commit may have landed.
            permissions.drop_views(server.id)
            slot.cancel()
        raise
    token = permissions.drop_views(server.id)
    slot.run(partial(announce_overwrite, request.app.state, OverwriteChange(
        server.id, server.owner_id, key, rows, bits, token)))
    return row_json(key, bits)


@router.get("/channels/{channel_id}/permissions")
async def list_channel_overwrites(
    channel_id: int, current_user: User = Depends(get_current_user),
):
    return await _listing(await _channel_target(channel_id, current_user), current_user)


@router.get("/channel-groups/{group_id}/permissions")
async def list_group_overwrites(
    group_id: int, current_user: User = Depends(get_current_user),
):
    return await _listing(await _group_target(group_id, current_user), current_user)


@router.put("/channels/{channel_id}/permissions/{kind}/{subject_id}")
async def set_channel_overwrite(
    channel_id: int, kind: Literal["roles", "members"], subject_id: int,
    body: OverwriteBody, request: Request, current_user: User = Depends(get_current_user),
):
    target = await _channel_target(channel_id, current_user)
    row = await _write(request, target, current_user, kind, subject_id, body)
    return row if row is not None else Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/channel-groups/{group_id}/permissions/{kind}/{subject_id}")
async def set_group_overwrite(
    group_id: int, kind: Literal["roles", "members"], subject_id: int,
    body: OverwriteBody, request: Request, current_user: User = Depends(get_current_user),
):
    target = await _group_target(group_id, current_user)
    row = await _write(request, target, current_user, kind, subject_id, body)
    return row if row is not None else Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/channels/{channel_id}/permissions/{kind}/{subject_id}",
    status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_overwrite(
    channel_id: int, kind: Literal["roles", "members"], subject_id: int,
    request: Request, current_user: User = Depends(get_current_user),
):
    target = await _channel_target(channel_id, current_user)
    await _write(request, target, current_user, kind, subject_id, OverwriteBody())


@router.delete(
    "/channel-groups/{group_id}/permissions/{kind}/{subject_id}",
    status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_overwrite(
    group_id: int, kind: Literal["roles", "members"], subject_id: int,
    request: Request, current_user: User = Depends(get_current_user),
):
    target = await _group_target(group_id, current_user)
    await _write(request, target, current_user, kind, subject_id, OverwriteBody())


@router.get("/channels/{channel_id}/viewers")
async def channel_viewers(channel_id: int, current_user: User = Depends(get_current_user)):
    """Who can view the channel, for the @mention list of a private channel."""
    target = await _channel_target(channel_id, current_user)
    return {"user_ids": sorted(await permissions.viewers(target.server.id, channel_id))}
