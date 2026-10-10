from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from ..models.Channel import Channel
from ..models.PermissionOverwrite import PermissionOverwrite
from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.UserToServer import UserToServer
from ..permissions import ALL_PERMISSIONS, Permission
from ..models.ChannelGroup import ChannelGroup
from .role_resolution import CHANNEL_BITS, Bits, RoleLike, channel_mask, member_mask


@dataclass
class TargetRows:
    """The overwrite rows of one channel or category."""
    roles: dict[int, Bits] = field(default_factory=dict)
    members: dict[int, Bits] = field(default_factory=dict)


TargetKind = Literal["channel", "group"]
SubjectKind = Literal["role", "member"]
NO_ROW: Bits = (0, 0)


@dataclass(frozen=True)
class RowKey:
    """Which overwrite row: its target and its subject."""
    target: TargetKind
    target_id: int
    subject: SubjectKind
    subject_id: int

    @property
    def filters(self) -> dict[str, int]:
        return {"channel_id" if self.target == "channel" else "group_id": self.target_id,
                "role_id" if self.subject == "role" else "user_id": self.subject_id}


@dataclass(frozen=True)
class OverwriteRows:
    """Every overwrite row of a server, by target."""
    groups: Mapping[int, TargetRows]
    channels: Mapping[int, TargetRows]

    def _side(self, key: RowKey) -> Mapping[int, TargetRows]:
        return self.channels if key.target == "channel" else self.groups

    def bits(self, key: RowKey) -> Bits:
        rows = self._side(key).get(key.target_id)
        if rows is None:
            return NO_ROW
        subjects = rows.roles if key.subject == "role" else rows.members
        return subjects.get(key.subject_id, NO_ROW)

    def with_row(self, key: RowKey, bits: Bits) -> "OverwriteRows":
        """These rows with one row set, or removed for NO_ROW. Untouched
        targets are shared, so nothing here may mutate a TargetRows."""
        side = dict(self._side(key))
        current = side.get(key.target_id)
        rows = TargetRows() if current is None else TargetRows(
            dict(current.roles), dict(current.members))
        subjects = rows.roles if key.subject == "role" else rows.members
        if bits == NO_ROW:
            subjects.pop(key.subject_id, None)
        else:
            subjects[key.subject_id] = bits
        if rows.roles or rows.members:
            side[key.target_id] = rows
        else:
            side.pop(key.target_id, None)
        if key.target == "channel":
            return replace(self, channels=side)
        return replace(self, groups=side)


@dataclass(frozen=True)
class ServerRules:
    """Everything a server's channel masks are computed from, loaded together."""
    owner_id: int
    roles: Mapping[int, RoleLike]
    channels: Mapping[int, int | None]
    group_ids: list[int]
    rows: OverwriteRows

    def layers(self, channel_id: int, user_id: int):
        group_id = self.channels.get(channel_id)
        for rows in (self.rows.groups.get(group_id) if group_id is not None else None,
                     self.rows.channels.get(channel_id)):
            if rows is not None:
                yield rows.roles, rows.members.get(user_id)

    def mask(self, user_id: int, server_mask: Permission, assigned, channel_id: int) -> Permission:
        if user_id == self.owner_id:
            return ALL_PERMISSIONS
        return channel_mask(
            server_mask, self.roles, assigned, self.layers(channel_id, user_id))

    def group_mask(self, user_id: int, server_mask: Permission, assigned, group_id: int) -> Permission:
        if user_id == self.owner_id:
            return ALL_PERMISSIONS
        rows = self.rows.groups.get(group_id)
        layers = [] if rows is None else [(rows.roles, rows.members.get(user_id))]
        return channel_mask(server_mask, self.roles, assigned, layers)

    def target_mask(
        self, user_id: int, server_mask: Permission, assigned, key: RowKey
    ) -> Permission:
        """The user's mask in the channel or category the row is on."""
        if key.target == "channel":
            return self.mask(user_id, server_mask, assigned, key.target_id)
        return self.group_mask(user_id, server_mask, assigned, key.target_id)

    def with_row(self, key: RowKey, bits: Bits) -> "ServerRules":
        return replace(self, rows=self.rows.with_row(key, bits))

    def private_flags(self) -> tuple[set[int], set[int]]:
        """The channels and the categories @everyone can't view."""
        return (
            {cid for cid in self.channels if self.is_private(channel_id=cid)},
            {gid for gid in self.group_ids if self.is_private(group_id=gid)},
        )

    def is_private(self, channel_id: int | None = None, group_id: int | None = None) -> bool:
        """Whether @everyone can't view the channel (or the category)."""
        everyone = member_mask(self.roles, [])
        if channel_id is not None:
            mask = self.mask(0, everyone, [], channel_id)
        else:
            mask = self.group_mask(0, everyone, [], group_id)
        return not mask & Permission.VIEW_CHANNEL

    def view(self, user_id: int, server_mask: Permission, assigned, group_ids) -> "MemberView":
        channels = {cid: self.mask(user_id, server_mask, assigned, cid) for cid in self.channels}
        groups = set()
        for group_id in group_ids:
            if (server_mask & Permission.MANAGE_CHANNELS
                    or self.group_mask(user_id, server_mask, assigned, group_id)
                    & Permission.VIEW_CHANNEL
                    or any(self.channels[cid] == group_id and mask & Permission.VIEW_CHANNEL
                           for cid, mask in channels.items())):
                groups.add(group_id)
        return MemberView(server_mask, channels, groups)


@dataclass
class MemberView:
    """What one member sees of a server: the server mask, the mask in every
    channel, and the categories shown to them."""
    mask: Permission
    channels: dict[int, Permission]
    groups: set[int]

    def can_view(self, channel_id: int) -> bool:
        return bool(self.channels.get(channel_id, Permission(0)) & Permission.VIEW_CHANNEL)

    def visible_channels(self) -> set[int]:
        return {cid for cid in self.channels if self.can_view(cid)}

    def differing_masks(self) -> dict[str, str]:
        """Masks in the channels the member can view that aren't simply the
        server mask, as the frames send them. Hidden channels are left out so
        their ids don't reach the member."""
        return {
            str(cid): str(int(mask)) for cid, mask in self.channels.items()
            if mask & Permission.VIEW_CHANNEL and mask != _plain_channel_mask(self.mask)}


def _plain_channel_mask(server_mask: Permission) -> Permission:
    """A channel mask with no overwrites in play."""
    if not server_mask & Permission.VIEW_CHANNEL:
        return Permission(int(server_mask) & ~CHANNEL_BITS)
    return server_mask


async def load_rows(server_id: int) -> OverwriteRows:
    groups: dict[int, TargetRows] = {}
    channels: dict[int, TargetRows] = {}
    for channel_id, group_id, role_id, user_id, allow, deny in await PermissionOverwrite.filter(
            server_id=server_id).values_list(
            "channel_id", "group_id", "role_id", "user_id", "allow", "deny"):
        target = (channels.setdefault(channel_id, TargetRows()) if channel_id is not None
                  else groups.setdefault(group_id, TargetRows()))
        if role_id is not None:
            target.roles[role_id] = (allow, deny)
        else:
            target.members[user_id] = (allow, deny)
    return OverwriteRows(groups, channels)


async def load_rules(server: Server) -> ServerRules:
    roles = {role.id: role for role in await Role.filter(server_id=server.id)}
    channels = dict(await Channel.filter(server_id=server.id).values_list("id", "group_id"))
    rows = await load_rows(server.id)
    group_ids = await ChannelGroup.filter(server_id=server.id).values_list("id", flat=True)
    return ServerRules(server.owner_id, roles, channels, list(group_ids), rows)


class PermissionResolver:
    """Effective permissions per (user, server) and per (user, channel), cached
    in memory.

    One process owns all sockets (single gunicorn worker, see deploy/gunicorn.conf.py),
    so an in-process cache is coherent. More workers would need shared invalidation.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], Permission] = {}
        self._channel_cache: dict[tuple[int, int], MemberView] = {}
        self._refill: dict[int, set[int]] = {}
        self._invalidations = 0

    async def effective(
        self, user_id: int, server: Server | int, *, channel_id: int | None = None
    ) -> Permission | None:
        """The user's permissions in *server*, or in one of its channels, or None
        when they aren't a member (or the channel isn't in the server)."""
        if isinstance(server, int):
            server = await Server.get_or_none(id=server)
            if server is None:
                return None
        if channel_id is not None:
            masks = await self.channel_masks(user_id, server)
            if masks is not None and channel_id not in masks:
                # Created after the view was cached (the importer adds channels
                # without going through the channel endpoints).
                self._channel_cache.pop((user_id, server.id), None)
                masks = await self.channel_masks(user_id, server)
            return None if masks is None else masks.get(channel_id)
        if server.owner_id == user_id:
            return ALL_PERMISSIONS

        # Not-a-member is never cached, so a join takes effect immediately.
        key = (user_id, server.id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not await UserToServer.filter(user_id=user_id, server_id=server.id).exists():
            return None

        invalidations = self._invalidations
        base = await self._server_level(user_id, server.id)
        # Skip caching when an invalidation landed while the roles were loading.
        if invalidations == self._invalidations:
            self._cache[key] = base
        return base

    async def channel_masks(self, user_id: int, server: Server) -> dict[int, Permission] | None:
        """The user's mask in every channel of *server*, or None for a non-member."""
        view = await self.view(user_id, server)
        return None if view is None else view.channels

    async def view(self, user_id: int, server: Server) -> "MemberView | None":
        """What the user sees of *server*, or None for a non-member."""
        key = (user_id, server.id)
        cached = self._channel_cache.get(key)
        if cached is not None:
            return cached
        invalidations = self._invalidations
        base = await self.effective(user_id, server)
        if base is None:
            return None
        rules = await load_rules(server)
        assigned = await RoleToUser.filter(
            user_id=user_id, role_id__in=list(rules.roles)).values_list("role_id", flat=True)
        view = rules.view(user_id, base, assigned, rules.group_ids)
        if invalidations == self._invalidations:
            self._channel_cache[key] = view
        return view

    async def has(
        self, user_id: int, server: Server | int, perm: Permission, *, channel_id: int | None = None
    ) -> bool:
        effective = await self.effective(user_id, server, channel_id=channel_id)
        return effective is not None and (effective & perm) == perm

    async def viewers(self, server_id: int, channel_id: int) -> set[int]:
        """Members who can view the channel. Not cached: it runs once per fan-out."""
        server = await Server.get_or_none(id=server_id)
        if server is None:
            return set()
        return await channel_viewers(server, channel_id)

    def invalidate(self, server_id: int, user_id: int | None = None) -> None:
        """Drop cached masks for one member, or for the whole server when *user_id* is None."""
        self._invalidations += 1
        for cache in (self._cache, self._channel_cache):
            if user_id is not None:
                cache.pop((user_id, server_id), None)
                continue
            for key in [k for k in cache if k[1] == server_id]:
                del cache[key]

    def drop_views(self, server_id: int) -> int:
        """Drop every member's channel view of the server, remembering whose to
        refill, and keep the server-level masks (overwrites can't change those).
        Returns the token refill_views needs."""
        self._invalidations += 1
        dropped = self._refill.setdefault(server_id, set())
        for key in [k for k in self._channel_cache if k[1] == server_id]:
            dropped.add(key[0])
            del self._channel_cache[key]
        return self._invalidations

    def refill_views(self, server_id: int, views: Mapping[int, MemberView], token: int) -> None:
        """Put back the dropped views, computed after the write, unless anything
        was invalidated since; then they load again on demand."""
        if token != self._invalidations:
            return
        for user_id in self._refill.pop(server_id, ()):
            if user_id in views:
                self._channel_cache[(user_id, server_id)] = views[user_id]

    def clear(self) -> None:
        self._invalidations += 1
        self._cache.clear()
        self._channel_cache.clear()
        self._refill.clear()

    @staticmethod
    async def _server_level(user_id: int, server_id: int) -> Permission:
        roles = {role.id: role for role in await Role.filter(server_id=server_id)}
        assigned = await RoleToUser.filter(
            user_id=user_id, role_id__in=list(roles)).values_list("role_id", flat=True)
        return member_mask(roles, assigned)


async def role_assignments(roles) -> dict[int, list[int]]:
    assigned: dict[int, list[int]] = {}
    for user_id, role_id in await RoleToUser.filter(
            role_id__in=list(roles)).values_list("user_id", "role_id"):
        assigned.setdefault(user_id, []).append(role_id)
    return assigned


async def server_masks(server: Server) -> dict[int, Permission]:
    """Every member's server-level mask, from one query each for roles,
    assignments and members."""
    roles = {role.id: role for role in await Role.filter(server_id=server.id)}
    assigned = await role_assignments(roles)
    member_ids = await UserToServer.filter(server_id=server.id).values_list("user_id", flat=True)
    return {
        user_id: ALL_PERMISSIONS if user_id == server.owner_id
        else member_mask(roles, assigned.get(user_id, []))
        for user_id in member_ids
    }


def views_of(
    rules: ServerRules, member_ids: Iterable[int], assigned: Mapping[int, list[int]]
) -> dict[int, MemberView]:
    """Every listed member's view under *rules*."""
    views = {}
    for user_id in member_ids:
        mine = assigned.get(user_id, [])
        base = ALL_PERMISSIONS if user_id == rules.owner_id else member_mask(rules.roles, mine)
        views[user_id] = rules.view(user_id, base, mine, rules.group_ids)
    return views


async def member_views(server: Server) -> dict[int, MemberView]:
    """Every member's view of *server*. Used for fan-out and to compare what
    members see before and after a change."""
    rules = await load_rules(server)
    assigned = await role_assignments(rules.roles)
    member_ids = await UserToServer.filter(server_id=server.id).values_list("user_id", flat=True)
    return views_of(rules, member_ids, assigned)


async def channel_viewers(server: Server, channel_id: int) -> set[int]:
    rules = await load_rules(server)
    if channel_id not in rules.channels:
        return set()
    assigned = await role_assignments(rules.roles)
    member_ids = await UserToServer.filter(server_id=server.id).values_list("user_id", flat=True)
    viewers = set()
    for user_id in member_ids:
        mine = assigned.get(user_id, [])
        base = ALL_PERMISSIONS if user_id == server.owner_id else member_mask(rules.roles, mine)
        if rules.mask(user_id, base, mine, channel_id) & Permission.VIEW_CHANNEL:
            viewers.add(user_id)
    return viewers


permissions = PermissionResolver()
