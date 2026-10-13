"""The Delete channels migration, run against the test database."""
import importlib.util
from pathlib import Path

import pytest
from tortoise import connections

from app.models.PermissionOverwrite import PermissionOverwrite
from app.models.Role import Role
from app.models.RoleToUser import RoleToUser
from app.models.Server import Server
from app.permissions import Permission
from app.services.permissions import permissions, server_masks
from tests.conftest import create_channel, create_server, register
from tests.test_permissions import join, run

MIGRATION = next(Path(__file__).parent.parent.glob("migrations/models/48_*.py"))
MANAGE = int(Permission.MANAGE_CHANNELS)
DELETE = int(Permission.DELETE_CHANNELS)
SEND = int(Permission.SEND_MESSAGES)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("delete_channels_permission", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(server_id, channel_id, members):
    """Roles that stress the copy, written in the state before the migration
    ran, and members holding between none and three of them."""
    def role(name, position, **fields):
        return Role.create(server_id=server_id, name=name, position=position, **fields)

    manager = await role("Manager", 10, allow=MANAGE)
    child = await role("Child", 11, parent_id=manager.id)
    blocked = await role("Blocked", 12, deny=MANAGE)
    override = await role("Override", 13, parent_id=blocked.id, allow=MANAGE)
    stale = await role("Stale", 14, allow=DELETE)
    clash = await role("Clash", 15, allow=DELETE, deny=MANAGE)
    stale_deny = await role("StaleDeny", 16, deny=DELETE)
    held = [
        [], [manager], [child], [manager, blocked], [override],
        [stale], [clash, manager], [stale, override], [manager, stale_deny],
    ]
    for user_id, roles in zip(members, held):
        await RoleToUser.bulk_create([RoleToUser(role_id=r.id, user_id=user_id) for r in roles])
    await PermissionOverwrite.create(
        server_id=server_id, channel_id=channel_id, role_id=manager.id, deny=SEND)


async def _masks(server_id):
    permissions.clear()
    server = await Server.get(id=server_id)
    channels = {
        user_id: (await permissions.channel_masks(user_id, server)) or {}
        for user_id in await server_masks(server)}
    return await server_masks(server), channels


async def _role_bits(server_id):
    return {r.name: (r.allow, r.deny) for r in await Role.filter(server_id=server_id)}


def test_every_member_keeps_their_mask_and_delete_follows_manage(client, new_client, migration):
    register(client)
    server = create_server(client, "Legacy")
    channel = create_channel(client, server["id"])
    members = [join(client, new_client, server["id"])[1]["id"] for _ in range(9)]

    async def upgrade():
        db = connections.get("default")
        await db.execute_script(await migration.downgrade(db))
        await _seed(server["id"], channel["id"], members)
        before = await _masks(server["id"]), await _role_bits(server["id"])
        await db.execute_script(await migration.upgrade(db))
        after = await _masks(server["id"]), await _role_bits(server["id"])
        await db.execute_script(await migration.downgrade(db))
        return before, after, await _role_bits(server["id"])

    ((before_server, before_channel), before_roles), (
        (after_server, after_channel), after_roles), downgraded = run(client, upgrade)

    assert {bool(before_server[m] & MANAGE) for m in members} == {True, False}
    assert any(before_server[m] & DELETE and not before_server[m] & MANAGE for m in members)
    for user_id in members:
        before, after = before_server[user_id], after_server[user_id]
        assert bool(after & DELETE) == bool(before & MANAGE), user_id
        assert after & ~DELETE == before & ~DELETE, user_id
        before, after = before_channel[user_id][channel["id"]], after_channel[user_id][channel["id"]]
        assert bool(after & DELETE) == bool(before & MANAGE), user_id
        assert after & ~DELETE == before & ~DELETE, user_id
    for name, (allow, deny) in after_roles.items():
        assert bool(allow & DELETE) == bool(allow & MANAGE), name
        assert bool(deny & DELETE) == bool(deny & MANAGE), name
        assert allow & deny & ~DELETE == before_roles[name][0] & before_roles[name][1] & ~DELETE
    assert not any(allow & DELETE or deny & DELETE for allow, deny in downgraded.values())
