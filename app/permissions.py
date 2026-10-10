from enum import IntFlag
from functools import reduce
from operator import or_
from typing import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, HTTPException

from .middleware import get_current_user
from .models.Channel import Channel
from .models.Invite import Invite
from .models.Server import Server
from .models.User import User


class Permission(IntFlag):
    # Stored in the DB and sent to clients. Never renumber; only append.
    # Bit 63 stays unused (signed BIGINT).
    VIEW_CHANNEL = 1 << 0
    SEND_MESSAGES = 1 << 1
    MANAGE_MESSAGES = 1 << 2
    CONNECT = 1 << 3
    SPEAK = 1 << 4
    STREAM = 1 << 5
    CREATE_INVITE = 1 << 6
    MANAGE_INVITES = 1 << 7
    MANAGE_CHANNELS = 1 << 8
    KICK_MEMBERS = 1 << 9
    MANAGE_SERVER = 1 << 10
    MANAGE_ROLES = 1 << 11
    MUTE_MEMBERS = 1 << 12
    MOVE_MEMBERS = 1 << 13


ALL_PERMISSIONS = reduce(or_, Permission)
MEMBER_PERMISSIONS = (
    Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES | Permission.CONNECT
    | Permission.SPEAK | Permission.STREAM | Permission.CREATE_INVITE
)
ADMIN_PERMISSIONS = (
    MEMBER_PERMISSIONS | Permission.MANAGE_MESSAGES | Permission.MANAGE_INVITES
    | Permission.MANAGE_CHANNELS | Permission.KICK_MEMBERS
    | Permission.MUTE_MEMBERS | Permission.MOVE_MEMBERS
)
DEFAULT_ROLE_NAME = "@everyone"
ADMIN_ROLE_NAME = "Admin"


async def require_permission(
    user: User, server: Server, perm: Permission, *, hide: bool = False
) -> Permission:
    """Raise unless *user* is a member of *server* with every bit of *perm*.

    Non-members get 403 "Not a member of this server", or 404 "Server not
    found" when hide=True (a 403 would confirm the server exists). Members
    missing a bit get 403 "Missing permission". Returns the effective mask.
    """
    # Imported here because the resolver imports this module for the enum.
    from .services.permissions import permissions

    effective = await permissions.effective(user.id, server)
    if effective is None:
        if hide:
            raise HTTPException(status_code=404, detail="Server not found")
        raise HTTPException(status_code=403, detail="Not a member of this server")
    if (effective & perm) != perm:
        raise HTTPException(status_code=403, detail="Missing permission")
    return effective


async def require_channel(
    user: User, channel: Channel, perm: Permission = Permission.VIEW_CHANNEL,
    *, not_found: str = "Channel not found", server: Server | None = None,
) -> Permission:
    """Raise unless *user* may do *perm* in *channel*. Non-members get the
    same 403 as require_permission, and so does a member without View in the
    whole server; a member a channel rule hides it from gets 404 *not_found*,
    so private channels can't be probed. Pass the channel's *server* when it is
    already loaded to save a query. Returns the mask."""
    from .services.permissions import permissions

    where = channel.server_id if server is None else server
    effective = await permissions.effective(user.id, where, channel_id=channel.id)
    if effective is None:
        if await permissions.effective(user.id, where) is None:
            raise HTTPException(status_code=403, detail="Not a member of this server")
        raise HTTPException(status_code=404, detail=not_found)
    if not effective & Permission.VIEW_CHANNEL:
        server_mask = await permissions.effective(user.id, where)
        if server_mask is not None and not server_mask & Permission.VIEW_CHANNEL:
            # No View anywhere in the server: nothing is hidden by a channel rule.
            raise HTTPException(status_code=403, detail="Missing permission")
        raise HTTPException(status_code=404, detail=not_found)
    if (effective & perm) != perm:
        raise HTTPException(status_code=403, detail="Missing permission")
    return effective


async def server_from_path(server_id: int) -> Server:
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


async def channel_from_path(channel_id: int) -> Channel:
    channel = await Channel.get_or_none(id=channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


async def server_of_channel(channel: Channel = Depends(channel_from_path)) -> Server:
    server = await Server.get_or_none(id=channel.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


async def invite_from_path(invite_id: UUID) -> Invite:
    invite = await Invite.get_or_none(id=invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    return invite


async def server_of_invite(invite: Invite = Depends(invite_from_path)) -> Server:
    server = await Server.get_or_none(id=invite.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


def check_permission(
    perm: Permission,
    resolve: Callable[..., Awaitable[Server]] = server_from_path,
    *,
    hide: bool = False,
):
    """FastAPI dependency factory:
    ``server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel))``.

    Resolves the server (404 if the path entity doesn't exist), checks the
    permission and hands the loaded server to the handler.
    """
    async def dependency(
        current_user: User = Depends(get_current_user),
        server: Server = Depends(resolve),
    ) -> Server:
        await require_permission(current_user, server, perm, hide=hide)
        return server

    return dependency


async def manage_invite(
    current_user: User = Depends(get_current_user),
    invite: Invite = Depends(invite_from_path),
    server: Server = Depends(server_of_invite),
) -> Invite:
    """Let MANAGE_INVITES holders and an invite's creator (while they still
    hold CREATE_INVITE) edit or revoke it."""
    if invite.created_by_id == current_user.id:
        try:
            await require_permission(current_user, server, Permission.CREATE_INVITE)
            return invite
        except HTTPException:
            pass
    await require_permission(current_user, server, Permission.MANAGE_INVITES)
    return invite


def check_channel(perm: Permission = Permission.VIEW_CHANNEL):
    """Like check_permission(perm, server_of_channel), checked against the
    channel's own mask."""
    async def dependency(
        current_user: User = Depends(get_current_user),
        channel: Channel = Depends(channel_from_path),
        server: Server = Depends(server_of_channel),
    ) -> Server:
        await require_channel(current_user, channel, perm)
        return server

    return dependency
