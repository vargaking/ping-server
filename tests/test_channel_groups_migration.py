"""The data step of the channel groups migration, run against the test database."""
import importlib.util
from pathlib import Path

import pytest
from tortoise import connections

from app.models.Channel import Channel
from app.models.ChannelGroup import ChannelGroup
from app.models.Server import Server
from tests.conftest import create_channel, create_server, register
from tests.test_permissions import run

MIGRATION = next(Path(__file__).parent.parent.glob("migrations/models/35_*_add_channel_groups.py"))


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("add_channel_groups", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _make_legacy(server_id, settings):
    """Put a server into its pre-migration state: no groups, channels with no
    group, and the given settings."""
    await Channel.filter(server_id=server_id).update(group_id=None, position=0)
    await ChannelGroup.filter(server_id=server_id).delete()
    await Server.filter(id=server_id).update(server_settings=settings)


async def _set_settings(server_id, settings):
    await Server.filter(id=server_id).update(server_settings=settings)


async def _migrate(migration):
    await migration.move_channels_into_groups(connections.get("default"))


async def _state(server_id):
    groups = await ChannelGroup.filter(server_id=server_id).order_by("position", "id")
    channels = await Channel.filter(server_id=server_id).order_by("position", "id")
    return {
        "groups": [(g.name, g.position) for g in groups],
        "members": [
            [(c.id, c.position) for c in channels if c.group_id == g.id] for g in groups],
        "ungrouped": [c.id for c in channels if c.group_id is None],
        "settings": (await Server.get(id=server_id)).server_settings,
    }


def _server_with_channels(client, name, kinds):
    server = create_server(client, name)
    channels = [create_channel(client, server["id"], f"c{i}", kind) for i, kind in enumerate(kinds)]
    return server["id"], [c["id"] for c in channels]


def test_partial_and_garbage_channel_order_keeps_the_effective_order(client, migration):
    register(client)
    sid, ids = _server_with_channels(
        client, "Legacy", ["text", "voice", "text", "text", "voice"])
    first, second, third, fourth, fifth = ids
    other_sid, other_ids = _server_with_channels(client, "Other", ["text"])
    run(client, _make_legacy, other_sid, {})
    run(client, _make_legacy, sid, {
        "channel_order": [fourth, 9999, str(third), second, fourth, True, other_ids[0], None],
        "default_channel_id": third,
        "keep": {"me": 1},
    })

    run(client, _migrate, migration)

    state = run(client, _state, sid)
    assert state["groups"] == [("Text channels", 0), ("Voice channels", 1)]
    # Effective order is [4, 2, 1, 3, 5]; text and voice keep their relative order.
    assert state["members"] == [
        [(fourth, 0), (first, 1), (third, 2)],
        [(second, 0), (fifth, 1)],
    ]
    assert state["ungrouped"] == []
    assert state["settings"] == {"default_channel_id": third, "keep": {"me": 1}}
    assert client.get(f"/servers/{sid}").json()["server_settings"]["channel_order"] == [
        fourth, first, third, second, fifth]


def test_server_without_channel_order_uses_id_order(client, migration):
    register(client)
    sid, ids = _server_with_channels(client, "Plain", ["voice", "text", "text"])
    run(client, _make_legacy, sid, {"theme": "dark"})

    run(client, _migrate, migration)

    state = run(client, _state, sid)
    assert state["members"] == [[(ids[1], 0), (ids[2], 1)], [(ids[0], 0)]]
    assert state["settings"] == {"theme": "dark"}


def test_channel_order_that_is_not_a_list_degrades_to_id_order(client, migration):
    register(client)
    sid, ids = _server_with_channels(client, "Odd", ["text", "text"])
    run(client, _make_legacy, sid, {"channel_order": "nonsense"})

    run(client, _migrate, migration)

    state = run(client, _state, sid)
    assert state["members"] == [[(ids[0], 0), (ids[1], 1)], []]
    assert state["settings"] == {}


def test_server_without_channels_still_gets_its_groups(client, migration):
    register(client)
    sid = create_server(client, "Empty")["id"]
    run(client, _make_legacy, sid, {"channel_order": [1, 2]})

    run(client, _migrate, migration)

    state = run(client, _state, sid)
    assert state["groups"] == [("Text channels", 0), ("Voice channels", 1)]
    assert state["members"] == [[], []]
    assert state["settings"] == {}


def test_servers_are_migrated_in_batches_without_mixing_channels(client, migration):
    register(client)
    migration.BATCH_SIZE = 2
    try:
        servers = [
            _server_with_channels(client, f"S{i}", ["text", "voice", "text"])
            for i in range(5)
        ]
        for sid, ids in servers:
            run(client, _make_legacy, sid, {"channel_order": list(reversed(ids))})

        run(client, _migrate, migration)
    finally:
        migration.BATCH_SIZE = 200

    for sid, ids in servers:
        state = run(client, _state, sid)
        assert state["members"] == [[(ids[2], 0), (ids[0], 1)], [(ids[1], 0)]]
        assert state["settings"] == {}


def test_downgrade_restores_channel_order(client, migration):
    register(client)
    sid, ids = _server_with_channels(client, "Back", ["text", "voice", "text", "text"])
    group_id = client.get(f"/servers/{sid}/channels").json()["groups"][0]["id"]
    for channel_id in ids[:2]:
        client.patch(f"/channels/{channel_id}", json={"group_id": group_id})
    voice_group = client.post(f"/servers/{sid}/channel-groups", json={"name": "Extra"}).json()
    client.patch(f"/channels/{ids[3]}", json={"group_id": voice_group["id"]})
    run(client, _set_settings, sid, {"default_channel_id": ids[0]})

    run(client, migration.restore_channel_order, connections.get("default"))

    settings = run(client, _state, sid)["settings"]
    assert settings == {
        "default_channel_id": ids[0],
        "channel_order": [ids[2], ids[0], ids[1], ids[3]],
    }
