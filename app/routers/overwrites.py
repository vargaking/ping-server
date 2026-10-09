from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator, model_validator
from tortoise.transactions import in_transaction

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
from ..services.channel_visibility import group_payload, private_flags, send_per_member, visibility_change
from ..services.permissions import fresh_mask, permissions
from ..services.role_resolution import CHANNEL_BITS
from ..services.roles import BITS_YOU_LACK, Standing, standing_of
from .channels import ChannelResponse

router = APIRouter(tags=["permission overwrites"])

LOSE_OWN_ACCESS = "You would lose access to this channel"
MEMBER_ABOVE_YOU = "That member's highest role is at or above yours"


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

    async def mask_of(self, user_id: int) -> Permission:
        if self.channel is not None:
            return await fresh_mask(user_id, self.server, channel_id=self.channel.id)
        return await fresh_mask(user_id, self.server, group_id=self.group.id)


async def _channel_target(channel_id: int, user: User) -> Target:
    channel = await Channel.get_or_none(id=channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    await require_channel(user, channel)
    return Target(await Server.get(id=channel.server_id), channel=channel)


async def _group_target(group_id: int, user: User) -> Target:
    group = await ChannelGroup.get_or_none(id=group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Category not found")
    server = await Server.get(id=group.server_id)
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


async def _announce_private_flags(request: Request, target: Target) -> None:
    """Channels and categories whose lock may have changed, to the members
    who can see them."""
    comms = getattr(request.app.state, "comms", None)
    if comms is None:
        return
    server = target.server
    private_channels, private_groups = await private_flags(server.id)
    if target.group is not None:
        group = target.group
        payload = group_payload(group, private_groups)
        await send_per_member(comms, server, lambda user_id, view: None if group.id not in view.groups
                              else {"type": "channel_group_updated", "server_id": server.id,
                                    "group": payload})
        channels = await Channel.filter(group_id=group.id)
    else:
        channels = [target.channel]
    for channel in channels:
        payload = ChannelResponse.from_channel(
            channel, private=channel.id in private_channels).model_dump(
            mode="json", exclude={"last_read_message_id", "last_message_id"})
        await comms.broadcast_to_channel(server.id, channel.id, {
            "type": "channel_updated", "server_id": server.id, "channel": payload})


async def _write(
    request: Request, target: Target, user: User,
    kind: Literal["roles", "members"], subject_id: int, body: OverwriteBody,
) -> dict | None:
    """Set one overwrite row (or delete it when both masks are empty) under the
    rules for who may edit, and announce what it changed."""
    server = target.server
    actor_mask = await require_permission(user, server, Permission.MANAGE_ROLES)
    roles = {role.id: role for role in await Role.filter(server_id=server.id)}
    standing = await standing_of(user, server, actor_mask, roles)
    await _check_subject(target, standing, roles, kind, subject_id)

    subject = {"role_id": subject_id} if kind == "roles" else {"user_id": subject_id}
    async with visibility_change(request.app.state, server):
        async with channel_layout.locked_server(server.id):
            row = await PermissionOverwrite.get_or_none(**target.filters, **subject)
            old_allow, old_deny = (row.allow, row.deny) if row else (0, 0)
            changed = (old_allow ^ body.allow) | (old_deny ^ body.deny)
            if not standing.is_owner and changed & ~int(await target.mask_of(user.id)):
                raise HTTPException(status_code=403, detail=BITS_YOU_LACK)

            async with in_transaction():
                if not body.allow and not body.deny:
                    if row is not None:
                        await row.delete()
                    row = None
                elif row is None:
                    row = await PermissionOverwrite.create(
                        server=server, **target.filters, **subject,
                        allow=body.allow, deny=body.deny)
                else:
                    row.allow, row.deny = body.allow, body.deny
                    await row.save(update_fields=["allow", "deny"])
                if not standing.is_owner and not (
                        await target.mask_of(user.id)) & Permission.VIEW_CHANNEL:
                    raise HTTPException(status_code=403, detail=LOSE_OWN_ACCESS)
    await _announce_private_flags(request, target)
    return None if row is None else _row_json(row)


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
