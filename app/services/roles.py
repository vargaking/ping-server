from ..models.Role import Role
from ..models.Server import Server
from ..permissions import (
    ADMIN_PERMISSIONS,
    ADMIN_ROLE_NAME,
    DEFAULT_ROLE_NAME,
    MEMBER_PERMISSIONS,
)


async def seed_server_roles(server: Server) -> None:
    """Give a new server its default @everyone role and the Admin role."""
    await Role.create(
        server=server, name=DEFAULT_ROLE_NAME, is_default=True,
        allow=int(MEMBER_PERMISSIONS))
    await Role.create(server=server, name=ADMIN_ROLE_NAME, allow=int(ADMIN_PERMISSIONS))
