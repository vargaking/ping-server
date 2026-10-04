from ..models.Role import Role
from ..models.RoleToUser import RoleToUser
from ..models.Server import Server
from ..models.UserToServer import UserToServer
from ..permissions import ALL_PERMISSIONS, Permission
from .role_resolution import member_mask


class PermissionResolver:
    """Effective permissions per (user, server), cached in memory.

    One process owns all sockets (single gunicorn worker, see deploy/gunicorn.conf.py),
    so an in-process cache is coherent. More workers would need shared invalidation.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], Permission] = {}
        self._invalidations = 0

    async def effective(
        self, user_id: int, server: Server | int, *, channel_id: int | None = None
    ) -> Permission | None:
        """The user's permissions in *server*, or None when they aren't a member."""
        if isinstance(server, int):
            server = await Server.get_or_none(id=server)
            if server is None:
                return None
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
        # Channel overwrites (group, then channel, then member) apply to base here.
        return base

    async def has(self, user_id: int, server: Server | int, perm: Permission) -> bool:
        effective = await self.effective(user_id, server)
        return effective is not None and (effective & perm) == perm

    def invalidate(self, server_id: int, user_id: int | None = None) -> None:
        """Drop cached masks for one member, or for the whole server when *user_id* is None."""
        self._invalidations += 1
        if user_id is not None:
            self._cache.pop((user_id, server_id), None)
            return
        for key in [k for k in self._cache if k[1] == server_id]:
            del self._cache[key]

    def clear(self) -> None:
        self._invalidations += 1
        self._cache.clear()

    @staticmethod
    async def _server_level(user_id: int, server_id: int) -> Permission:
        roles = {role.id: role for role in await Role.filter(server_id=server_id)}
        assigned = await RoleToUser.filter(
            user_id=user_id, role_id__in=list(roles)).values_list("role_id", flat=True)
        return member_mask(roles, assigned)


async def server_masks(server: Server) -> dict[int, Permission]:
    """Every member's server-level mask, from one query each for roles,
    assignments and members."""
    roles = {role.id: role for role in await Role.filter(server_id=server.id)}
    assigned: dict[int, list[int]] = {}
    for user_id, role_id in await RoleToUser.filter(
            role_id__in=list(roles)).values_list("user_id", "role_id"):
        assigned.setdefault(user_id, []).append(role_id)
    member_ids = await UserToServer.filter(server_id=server.id).values_list("user_id", flat=True)
    return {
        user_id: ALL_PERMISSIONS if user_id == server.owner_id
        else member_mask(roles, assigned.get(user_id, []))
        for user_id in member_ids
    }


permissions = PermissionResolver()
