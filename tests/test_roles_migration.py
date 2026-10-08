"""The data step of the role position migration, run against the test database."""
import importlib.util
from pathlib import Path

import pytest
from tortoise import connections

from app.models.Role import Role
from app.models.RoleToUser import RoleToUser
from app.permissions import ADMIN_PERMISSIONS, Permission
from app.services.permissions import permissions, server_masks
from app.models.Server import Server
from tests.conftest import create_server, register
from tests.test_permissions import join, run

MIGRATION = next(Path(__file__).parent.parent.glob("migrations/models/37_*_role_position_color.py"))


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("role_position_color", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def old_mask(roles, assigned_ids):
    """The union algorithm the resolver used before roles had positions."""
    by_id = {role.id: role for role in roles}
    applied = [r.id for r in roles if r.is_default] + list(assigned_ids)
    allow = deny = 0
    seen = set()
    for role_id in applied:
        while role_id is not None and role_id not in seen:
            seen.add(role_id)
            role = by_id.get(role_id)
            if role is None:
                break
            allow |= role.allow
            deny |= role.deny
            role_id = role.parent_id
    return Permission(allow & ~deny)


async def _legacy_server(server_id, members):
    """Put a server into its pre-migration state: every role at position 0, an
    inheriting role, and members holding between none and three roles."""
    admin = await Role.get(server_id=server_id, name="Admin")
    mod = await Role.create(server_id=server_id, name="Mod", allow=int(Permission.MANAGE_ROLES))
    helper = await Role.create(
        server_id=server_id, name="Helper", allow=int(Permission.MANAGE_SERVER), parent_id=mod.id)
    cosmetic = await Role.create(server_id=server_id, name="Cosmetic")
    await Role.filter(server_id=server_id).update(position=0)
    held = [[], [admin.id], [helper.id], [mod.id, cosmetic.id], [admin.id, helper.id, cosmetic.id]]
    for user_id, role_ids in zip(members, held):
        await RoleToUser.bulk_create([RoleToUser(role_id=r, user_id=user_id) for r in role_ids])
    return held


async def _masks(server_id):
    server = await Server.get(id=server_id)
    return await server_masks(server)


async def _expected_masks(server_id):
    roles = await Role.filter(server_id=server_id)
    assigned: dict[int, list[int]] = {}
    for user_id, role_id in await RoleToUser.filter(
            role_id__in=[r.id for r in roles]).order_by("assigned_at", "id").values_list(
            "user_id", "role_id"):
        assigned.setdefault(user_id, []).append(role_id)
    return {user_id: old_mask(roles, ids) for user_id, ids in assigned.items()}, roles


async def _positions(server_id):
    roles = await Role.filter(server_id=server_id).order_by("id")
    return [(r.name, r.position) for r in roles]


def test_every_member_keeps_exactly_the_mask_they_had(client, new_client, migration):
    register(client)
    server = create_server(client, "Legacy")
    other = create_server(client, "Other")
    members = [join(client, new_client, server["id"])[1]["id"] for _ in range(5)]
    held = run(client, _legacy_server, server["id"], members)
    assert held[4]
    run(client, _legacy_server, other["id"], [])
    before_positions = run(client, _positions, server["id"])
    assert {position for _, position in before_positions} == {0}
    expected, roles = run(client, _expected_masks, server["id"])
    assert expected[members[1]] == ADMIN_PERMISSIONS
    assert expected[members[2]] != expected[members[3]]

    run(client, migration.number_roles, connections.get("default"))
    permissions.clear()

    masks = run(client, _masks, server["id"])
    for user_id in members:
        old = expected.get(user_id)
        if old is None:
            old = old_mask(roles, [])
        assert masks[user_id] == old, user_id
        assert run(client, permissions.effective, user_id, server["id"]) == old


def test_positions_are_everyone_zero_then_one_to_n_in_id_order_per_server(
        client, migration):
    register(client)
    first = create_server(client, "First")
    second = create_server(client, "Second")
    run(client, _legacy_server, first["id"], [])
    run(client, _legacy_server, second["id"], [])

    run(client, migration.number_roles, connections.get("default"))

    for server in (first, second):
        assert run(client, _positions, server["id"]) == [
            ("@everyone", 0), ("Admin", 1), ("Mod", 2), ("Helper", 3), ("Cosmetic", 4)]
