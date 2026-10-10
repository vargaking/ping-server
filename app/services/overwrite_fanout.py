from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.Role import Role
from ..models.UserToServer import UserToServer
from ..permissions import Permission
from . import channel_layout
from .channel_visibility import ServerState, announce_visibility
from .permissions import (
    NO_ROW,
    MemberView,
    OverwriteRows,
    RowKey,
    ServerRules,
    permissions,
    role_assignments,
    views_of,
)
from .role_resolution import Bits


@dataclass(frozen=True)
class OverwriteChange:
    """A saved overwrite write, as its fan-out needs it."""
    server_id: int
    owner_id: int
    key: RowKey
    rows: OverwriteRows  # every row of the server right before the write
    bits: Bits  # the row's new (allow, deny); NO_ROW when it was removed
    token: int  # from permissions.drop_views, for the refill guard


def row_json(key: RowKey, bits: Bits) -> dict | None:
    """A row as the API shows it, or None when there isn't one."""
    if bits == NO_ROW:
        return None
    subject = {"role_id": key.subject_id} if key.subject == "role" else {"user_id": key.subject_id}
    return {**subject, "allow": str(bits[0]), "deny": str(bits[1])}


def overwrite_frame(change: OverwriteChange) -> dict:
    key = change.key
    return {
        "type": "permission_overwrite_updated", "server_id": change.server_id,
        "target": key.target, "target_id": key.target_id,
        "subject": key.subject, "subject_id": key.subject_id,
        "overwrite": row_json(key, change.bits),
    }


def _private_flips(
    before: ServerRules, after: ServerRules, key: RowKey, channels: Mapping[int, Channel]
) -> tuple[set[int], set[int]]:
    """The channels and the category whose private flag the write changed."""
    if key.target == "channel":
        affected = [key.target_id] if key.target_id in channels else []
    else:
        affected = [cid for cid, channel in channels.items() if channel.group_id == key.target_id]
    flipped_channels = {
        cid for cid in affected
        if before.is_private(channel_id=cid) != after.is_private(channel_id=cid)}
    flipped_groups = set()
    if key.target == "group" and key.target_id in before.group_ids:
        if before.is_private(group_id=key.target_id) != after.is_private(group_id=key.target_id):
            flipped_groups.add(key.target_id)
    return flipped_channels, flipped_groups


async def _send_row(app_state: Any, change: OverwriteChange, views: Mapping[int, MemberView]) -> None:
    comms = getattr(app_state, "comms", None)
    if comms is None:
        return
    key = change.key

    def sees_target(view: MemberView) -> bool:
        return view.can_view(key.target_id) if key.target == "channel" else key.target_id in view.groups

    readers = sorted(
        user_id for user_id, view in views.items()
        if view.mask & Permission.MANAGE_ROLES and sees_target(view))
    await comms.send_to_users(readers, overwrite_frame(change))


async def announce_overwrite(app_state: Any, change: OverwriteChange) -> None:
    """Tell members what one saved overwrite changed for them and the role
    managers who can see its target the new row, then refill the views the
    write dropped. Runs after the response, in the server's queue."""
    server_id = change.server_id
    roles = {role.id: role for role in await Role.filter(server_id=server_id)}
    channels = await Channel.filter(server_id=server_id).order_by("position", "id")
    groups = await ChannelGroup.filter(server_id=server_id).order_by("position", "id")
    member_ids = await UserToServer.filter(server_id=server_id).values_list("user_id", flat=True)
    assigned = await role_assignments(roles)

    before = ServerRules(
        change.owner_id, roles, {channel.id: channel.group_id for channel in channels},
        [group.id for group in groups], change.rows)
    after = before.with_row(change.key, change.bits)
    old_views = views_of(before, member_ids, assigned)
    new_views = views_of(after, member_ids, assigned)
    permissions.refill_views(server_id, new_views, change.token)

    by_id = {channel.id: channel for channel in channels}
    flipped_channels, flipped_groups = _private_flips(before, after, change.key, by_id)
    private_channels, private_groups = after.private_flags()
    state = ServerState(
        by_id, {group.id: group for group in groups}, channel_layout.layout_of(channels, groups),
        private_channels, private_groups)
    await announce_visibility(
        app_state, server_id, state, old_views, new_views,
        flipped_channels=flipped_channels, flipped_groups=flipped_groups)
    await _send_row(app_state, change, new_views)
