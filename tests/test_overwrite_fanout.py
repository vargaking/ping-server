"""An overwrite write answers after a handful of queries; its frames and the
cache refill follow in the server's queue."""
import asyncio
import logging
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager

from app.models.Channel import Channel
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.permissions import Permission
from app.routers import overwrites as overwrites_router
from app.routers.overwrites import MEMBER_ABOVE_YOU
from app.services import channel_layout
from app.services.permissions import permissions
from app.services.roles import ABOVE_YOUR_RANK, BITS_YOU_LACK
from app.services.server_queue import server_queue
from app.services.voice_presence import VoiceParticipant
from tests.conftest import create_channel, create_server, drain_jobs, register, ws_ready
from tests.test_channel_permissions import (  # noqa: F401
    HEADERS, SEND, VIEW, crew, overwrite, post_message)
from tests.test_permissions import join, roles_by_name, run, set_roles
from tests.test_server_timing import query_count
from tests.test_voice_moderation import use_presence

LOGGER = "app.server_queue"
ROW_FRAME = "permission_overwrite_updated"
CONNECT = int(Permission.CONNECT)
SPEAK = int(Permission.SPEAK)
MANAGE_MESSAGES = int(Permission.MANAGE_MESSAGES)
LOSE_OWN_ACCESS = "You would lose access to this channel"
PRESENCE_NOISE = {"presence_update"}


def put(client, path, allow=0, deny=0):
    """A write without waiting for what follows its response."""
    return client.put(path, json={"allow": str(allow), "deny": str(deny)})


def channel_rule(channel_id, kind, subject_id):
    return f"/channels/{channel_id}/permissions/{kind}/{subject_id}"


def group_rule(group_id, kind, subject_id):
    return f"/channel-groups/{group_id}/permissions/{kind}/{subject_id}"


def warm(client, sid):
    drain_jobs(client)
    assert client.get(f"/servers/{sid}/channels").status_code == 200


def add_members(client, sid, count):
    async def add():
        for index in range(count):
            user = await User.create(username=f"filler{sid}_{index}", password_hash="x")
            await UserToServer.create(user=user, server_id=sid)

    run(client, add)


def job_query_counts(caplog) -> list[int]:
    return [record.queries for record in caplog.records if hasattr(record, "queries")]


def of_type(frames, frame_type):
    return [frame for frame in frames if frame["type"] == frame_type]


def types(frames):
    return [frame["type"] for frame in frames]


def row_frame(sid, target, target_id, subject, subject_id, overwrite_row):
    return {
        "type": ROW_FRAME, "server_id": sid, "target": target, "target_id": target_id,
        "subject": subject, "subject_id": subject_id, "overwrite": overwrite_row}


class Listeners:
    """The open sockets of a test, and what each received since the last settle."""

    def __init__(self, client, sockets):
        self.client = client
        self.sockets = sockets
        self.pings = 0

    def settle(self) -> dict[str, list[dict]]:
        """Wait for the queued work, then collect every socket's frames up to a
        ping answered after it, so nothing of the write is still on its way."""
        drain_jobs(self.client)
        self.pings += 1
        frames = {}
        for name, ws in self.sockets.items():
            ws.send_json({"type": "ping", "t": self.pings})
            received = []
            while True:
                frame = ws.receive_json()
                if frame == {"type": "pong", "t": self.pings}:
                    break
                if frame["type"] not in PRESENCE_NOISE:
                    received.append(frame)
            frames[name] = received
        return frames


@contextmanager
def listening(client, **clients):
    """Sockets for the given clients (the first is the owner's), with the
    frames of connecting already consumed."""
    with ExitStack() as stack:
        sockets = {}
        for name, member_client in clients.items():
            ws = stack.enter_context(member_client.websocket_connect("/ws", headers=HEADERS))
            ws_ready(ws)
            sockets[name] = ws
        listeners = Listeners(client, sockets)
        listeners.settle()
        yield listeners


@contextmanager
def held_queue(client, sid):
    """Keep the server's queue from running its jobs until released."""
    async def reserve():
        return server_queue.reserve(sid)

    slot = client.portal.call(reserve)
    released = []

    def release():
        if not released:
            released.append(True)

            async def cancel():
                slot.cancel()

            client.portal.call(cancel)

    try:
        yield release
    finally:
        release()


def category_server(client, name, channels, members):
    sid = create_server(client, name)["id"]
    group = client.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    for index in range(channels):
        res = client.post(f"/channels/{sid}/create", json={
            "name": f"c{index}", "group_id": group["id"]})
        assert res.status_code == 201, res.text
    add_members(client, sid, members)
    return sid, group["id"], roles_by_name(client, sid)["@everyone"]["id"]


# -- the request is cheap ---------------------------------------------------------

def test_owner_channel_put_runs_at_most_ten_queries(crew):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    add_members(owner, sid, 30)
    everyone = channel_rule(general, "roles", crew.everyone["id"])
    mod = channel_rule(general, "roles", crew.mod_role["id"])
    writes = [("private", everyone, {"deny": VIEW}), ("send for a role", mod, {"deny": SEND}),
              ("public again", everyone, {})]
    for label, path, bits in writes:
        warm(owner, sid)
        res = put(owner, path, **bits)
        assert res.status_code in (200, 204), res.text
        assert query_count(res) <= 10, label


def test_category_and_public_again_cost_the_same_whatever_their_size(client):
    register(client)
    counts = {}
    for index, (channels, members) in enumerate([(1, 6), (6, 6), (6, 60)]):
        sid, group, everyone = category_server(client, f"Server {index}", channels, members)
        path = group_rule(group, "roles", everyone)
        for label, bits in (("private", {"deny": VIEW}), ("public again", {})):
            warm(client, sid)
            counts.setdefault(label, []).append(query_count(put(client, path, **bits)))
    for label, seen in counts.items():
        assert len(set(seen)) == 1 and seen[0] <= 10, (label, seen)


def test_a_non_owner_write_loads_the_rows_once(crew):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    low = owner.post(f"/servers/{sid}/roles", json={"name": "Low"}).json()
    warm(owner, sid)
    assert crew.mod_client.get(f"/servers/{sid}/channels").status_code == 200
    res = put(crew.mod_client, channel_rule(general, "roles", low["id"]), deny=SEND)
    assert res.status_code == 200, res.text
    assert query_count(res) <= 10


# -- a write that changes nothing ---------------------------------------------------

def test_a_put_that_changes_nothing_sends_no_frames_and_keeps_the_cache(crew, caplog):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    path = channel_rule(general, "roles", crew.everyone["id"])
    first = overwrite(owner, path, deny=SEND)
    assert first.status_code == 200

    def member_reads():
        return query_count(crew.member_client.get(f"/servers/{sid}/channels"))

    member_reads()
    cached_cost = member_reads()
    warm(owner, sid)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER), \
            listening(owner, owner=owner, member=crew.member_client) as live:
        again = put(owner, path, deny=SEND)
        assert again.status_code == 200
        assert again.json() == first.json()
        assert query_count(again) <= 5

        missing = owner.delete(channel_rule(general, "roles", crew.mod_role["id"]))
        assert missing.status_code == 204
        assert query_count(missing) <= 5

        assert live.settle() == {"owner": [], "member": []}
    assert job_query_counts(caplog) == []
    assert member_reads() == cached_cost


def test_a_put_that_changes_nothing_still_checks_permissions(crew, new_client):
    sid, general = crew.sid, crew.general["id"]
    admin = roles_by_name(crew.owner_client, sid)["Admin"]
    stranger = register(new_client())

    res = put(crew.mod_client, channel_rule(general, "roles", admin["id"]))
    assert (res.status_code, res.json()["detail"]) == (403, ABOVE_YOUR_RANK)
    res = put(crew.mod_client, channel_rule(general, "members", crew.owner["id"]))
    assert (res.status_code, res.json()["detail"]) == (403, MEMBER_ABOVE_YOU)
    res = put(crew.owner_client, channel_rule(general, "members", stranger["id"]))
    assert (res.status_code, res.json()["detail"]) == (404, "Member not found")
    res = put(crew.owner_client, channel_rule(general, "roles", 99999))
    assert (res.status_code, res.json()["detail"]) == (404, "Role not found")
    res = put(crew.member_client, channel_rule(general, "roles", crew.everyone["id"]))
    assert (res.status_code, res.json()["detail"]) == (403, "Missing permission")
    res = put(crew.member_client, channel_rule(crew.secret["id"], "roles", crew.everyone["id"]))
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Channel not found"


# -- caches ---------------------------------------------------------------------------

def test_reads_right_after_a_write_see_it(crew):
    sid, general = crew.sid, crew.general["id"]
    assert crew.member_client.get(f"/channels/{general}/messages").status_code == 200
    res = put(crew.owner_client, channel_rule(general, "roles", crew.everyone["id"]), deny=VIEW)
    assert res.status_code == 200
    assert crew.member_client.get(f"/channels/{general}/messages").status_code == 404
    snapshot = crew.member_client.get(f"/servers/{sid}/channels").json()
    assert general not in [channel["id"] for channel in snapshot["channels"]]


def test_the_job_refills_the_views_it_dropped_and_keeps_server_masks(crew):
    sid, general = crew.sid, crew.general["id"]

    def member_reads():
        return query_count(crew.member_client.get(f"/servers/{sid}/channels"))

    member_reads()
    cached_cost = member_reads()
    res = put(crew.owner_client, channel_rule(general, "roles", crew.mod_role["id"]), deny=SEND)
    assert res.status_code == 200
    assert (crew.member["id"], sid) in permissions._cache
    drain_jobs(crew.owner_client)
    assert member_reads() == cached_cost


def test_a_quick_double_change_refills_every_dropped_view(crew):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    path = channel_rule(general, "roles", crew.mod_role["id"])

    def member_reads():
        return query_count(crew.member_client.get(f"/servers/{sid}/channels"))

    member_reads()
    cached_cost = member_reads()
    with held_queue(owner, sid) as release:
        assert put(owner, path, deny=SEND).status_code == 200
        assert put(owner, path, deny=SEND | SPEAK).status_code == 200
        release()
        drain_jobs(owner)
    assert member_reads() == cached_cost


def test_without_comms_a_write_still_saves_and_refills(crew, monkeypatch, caplog):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]

    def member_reads():
        return query_count(crew.member_client.get(f"/servers/{sid}/channels"))

    member_reads()
    cached_cost = member_reads()
    monkeypatch.setattr(owner.app.state, "comms", None)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        res = put(owner, channel_rule(general, "roles", crew.mod_role["id"]), deny=SEND)
        assert res.status_code == 200
        drain_jobs(owner)
    assert [r for r in caplog.records if r.name == LOGGER] == []
    assert member_reads() == cached_cost


# -- frames -----------------------------------------------------------------------------

def test_channel_updated_only_when_the_private_flag_flips(crew, new_client):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    keeper_client, keeper = join(owner, new_client, sid)
    everyone = channel_rule(general, "roles", crew.everyone["id"])
    assert overwrite(owner, channel_rule(general, "members", keeper["id"]), allow=VIEW).status_code == 200

    with listening(owner, owner=owner, keeper=keeper_client, other=crew.member_client) as live:
        assert overwrite(owner, everyone, deny=VIEW).status_code == 200
        frames = live.settle()
        listed = next(c for c in owner.get(f"/channels/{sid}").json() if c["id"] == general)
        expected = {k: v for k, v in listed.items()
                    if k not in ("last_read_message_id", "last_message_id")}
        assert expected["private"] is True
        for name in ("owner", "keeper"):
            assert of_type(frames[name], "channel_updated") == [{
                "type": "channel_updated", "server_id": sid, "channel": expected}]
        assert types(frames["owner"]).index("channel_updated") < types(
            frames["owner"]).index(ROW_FRAME)
        assert types(frames["other"]) == ["channel_deleted"]

        assert overwrite(owner, channel_rule(general, "roles", crew.mod_role["id"]),
                         deny=SEND).status_code == 200
        frames = live.settle()
        assert not any(of_type(fs, "channel_updated") for fs in frames.values())

        assert overwrite(owner, everyone).status_code == 204
        frames = live.settle()
        assert types(of_type(frames["other"], "channel_created")) == ["channel_created"]
        assert of_type(frames["other"], "channel_created")[0]["channel"]["private"] is False
        assert not of_type(frames["other"], "channel_updated")
        for name in ("owner", "keeper"):
            updated = of_type(frames[name], "channel_updated")
            assert [f["channel"]["private"] for f in updated] == [False]
            assert not of_type(frames[name], "channel_created")


def test_channel_group_updated_only_when_the_category_flips(crew, new_client):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    plans = owner.post(f"/channels/{sid}/create", json={
        "name": "plans", "group_id": group["id"]}).json()
    keeper_client, keeper = join(owner, new_client, sid)
    everyone = group_rule(group["id"], "roles", crew.everyone["id"])
    assert overwrite(owner, group_rule(group["id"], "members", keeper["id"]),
                     allow=VIEW).status_code == 200

    with listening(owner, owner=owner, keeper=keeper_client, other=crew.member_client) as live:
        assert overwrite(owner, everyone, deny=VIEW).status_code == 200
        frames = live.settle()
        listed = next(g for g in owner.get(f"/servers/{sid}/channels").json()["groups"]
                      if g["id"] == group["id"])
        assert listed["private"] is True
        for name in ("owner", "keeper"):
            assert of_type(frames[name], "channel_group_updated") == [{
                "type": "channel_group_updated", "server_id": sid, "group": listed}]
            assert [f["channel"]["id"] for f in of_type(frames[name], "channel_updated")] == [
                plans["id"]]
        assert types(frames["other"]) == ["channel_deleted", "channel_group_deleted"]

        assert overwrite(owner, group_rule(group["id"], "roles", crew.mod_role["id"]),
                         deny=SEND).status_code == 200
        frames = live.settle()
        assert not any(of_type(fs, "channel_group_updated") or of_type(fs, "channel_updated")
                       for fs in frames.values())

        assert overwrite(owner, everyone).status_code == 204
        frames = live.settle()
        assert types(frames["other"])[:2] == ["channel_group_created", "channel_created"]
        assert not of_type(frames["other"], "channel_group_updated")
        for name in ("owner", "keeper"):
            updated = of_type(frames[name], "channel_group_updated")
            assert [f["group"]["private"] for f in updated] == [False]


def test_a_category_write_leaves_channels_with_their_own_rule_alone(crew, new_client):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    ids = [owner.post(f"/channels/{sid}/create", json={
        "name": name, "group_id": group["id"]}).json()["id"] for name in ("one", "two", "open")]
    one, two, open_one = ids
    keeper_client, keeper = join(owner, new_client, sid)
    assert overwrite(owner, group_rule(group["id"], "members", keeper["id"]),
                     allow=VIEW).status_code == 200
    assert overwrite(owner, channel_rule(open_one, "roles", crew.everyone["id"]),
                     allow=VIEW).status_code == 200

    with listening(owner, owner=owner, keeper=keeper_client, other=crew.member_client) as live:
        res = overwrite(owner, group_rule(group["id"], "roles", crew.everyone["id"]), deny=VIEW)
        assert res.status_code == 200
        frames = live.settle()
        assert [(f["type"], f.get("channel_id")) for f in frames["other"]] == [
            ("channel_deleted", one), ("channel_deleted", two), ("channel_group_updated", None)]
        assert of_type(frames["other"], "channel_group_updated")[0]["group"]["private"] is True
        for name in ("owner", "keeper"):
            updated = of_type(frames[name], "channel_updated")
            assert sorted(f["channel"]["id"] for f in updated) == [one, two]
            assert open_one not in [f["channel"]["id"] for f in updated]
            assert len(of_type(frames[name], "channel_group_updated")) == 1
            assert not of_type(frames[name], "channel_deleted")


def test_a_gained_channel_carries_its_read_state(crew):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    first = post_message(owner, sid, general, "one")
    second = post_message(owner, sid, general, "two")
    res = crew.member_client.put(f"/channels/{general}/read", json={"message_id": first})
    assert res.status_code == 200, res.text
    path = channel_rule(general, "roles", crew.everyone["id"])
    assert overwrite(owner, path, deny=VIEW).status_code == 200

    with listening(owner, owner=owner, member=crew.member_client) as live:
        assert overwrite(owner, path).status_code == 204
        created = of_type(live.settle()["member"], "channel_created")
    assert len(created) == 1
    assert created[0]["channel"]["last_read_message_id"] == first
    assert created[0]["channel"]["last_message_id"] == second


def test_overwrite_row_frame_reaches_role_managers_who_see_the_target(crew):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    low = owner.post(f"/servers/{sid}/roles", json={"name": "Low"}).json()
    vault = create_channel(owner, sid, "vault")["id"]
    assert overwrite(owner, channel_rule(vault, "roles", crew.everyone["id"]),
                     deny=VIEW).status_code == 200
    path = channel_rule(general, "roles", low["id"])

    with listening(owner, owner=owner, owner_tab=owner, mod=crew.mod_client,
                   member=crew.member_client) as live:
        res = overwrite(owner, path, deny=SEND)
        row = {"role_id": low["id"], "allow": "0", "deny": str(SEND)}
        expected = row_frame(sid, "channel", general, "role", low["id"], row)
        frames = live.settle()
        for name in ("owner", "owner_tab", "mod"):
            assert of_type(frames[name], ROW_FRAME) == [expected]
        assert of_type(frames["member"], ROW_FRAME) == []
        assert res.json() == row
        assert owner.get(f"/channels/{general}/permissions").json()["roles"] == [row]

        assert owner.delete(path).status_code == 204
        removed = row_frame(sid, "channel", general, "role", low["id"], None)
        frames = live.settle()
        for name in ("owner", "owner_tab", "mod"):
            assert of_type(frames[name], ROW_FRAME) == [removed]

        hidden = overwrite(owner, channel_rule(vault, "roles", low["id"]), deny=SEND)
        hidden_row = {"role_id": low["id"], "allow": "0", "deny": str(SEND)}
        frames = live.settle()
        for name in ("owner", "owner_tab"):
            assert of_type(frames[name], ROW_FRAME) == [
                row_frame(sid, "channel", vault, "role", low["id"], hidden_row)]
        assert hidden.status_code == 200
        assert of_type(frames["mod"], ROW_FRAME) == []

        assert overwrite(owner, channel_rule(vault, "roles", low["id"]), deny=SEND).status_code == 200
        assert not any(of_type(fs, ROW_FRAME) for fs in live.settle().values())


def test_overwrite_row_frame_names_a_category_and_a_member(crew):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    path = group_rule(group["id"], "members", crew.member["id"])

    with listening(owner, owner=owner, mod=crew.mod_client, member=crew.member_client) as live:
        assert overwrite(owner, path, deny=SEND).status_code == 200
        row = {"user_id": crew.member["id"], "allow": "0", "deny": str(SEND)}
        expected = row_frame(sid, "group", group["id"], "member", crew.member["id"], row)
        frames = live.settle()
        assert of_type(frames["owner"], ROW_FRAME) == [expected]
        assert of_type(frames["mod"], ROW_FRAME) == [expected]
        assert of_type(frames["member"], ROW_FRAME) == []

        assert owner.delete(path).status_code == 204
        frames = live.settle()
        assert of_type(frames["owner"], ROW_FRAME) == [
            row_frame(sid, "group", group["id"], "member", crew.member["id"], None)]


def test_two_quick_writes_fan_out_in_commit_order(crew, monkeypatch):
    owner, general = crew.owner_client, crew.general["id"]
    original = overwrites_router.announce_overwrite
    calls = []

    async def slow_first(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            await asyncio.sleep(0.3)
        await original(*args, **kwargs)

    monkeypatch.setattr(overwrites_router, "announce_overwrite", slow_first)
    path = channel_rule(general, "roles", crew.mod_role["id"])
    with listening(owner, owner=owner) as live:
        for bits in (SEND, SEND | SPEAK):
            started = time.monotonic()
            assert put(owner, path, deny=bits).status_code == 200
            assert time.monotonic() - started < 0.3
        frames = live.settle()["owner"]
    assert [f["overwrite"]["deny"] for f in of_type(frames, ROW_FRAME)] == [
        str(SEND), str(SEND | SPEAK)]
    assert owner.get(f"/channels/{general}/permissions").json()["roles"][-1]["deny"] == str(
        SEND | SPEAK)


def test_a_failing_fan_out_is_logged_and_the_write_stands(crew, monkeypatch, caplog):
    owner, general = crew.owner_client, crew.general["id"]
    comms = owner.app.state.comms

    async def broken(*args, **kwargs):
        raise RuntimeError("socket layer is down")

    monkeypatch.setattr(comms, "send_to_user", broken)
    monkeypatch.setattr(comms, "send_to_users", broken)
    everyone = channel_rule(general, "roles", crew.everyone["id"])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert put(owner, everyone, deny=VIEW).status_code == 200
        drain_jobs(owner)
    failures = [r for r in caplog.records if r.name == LOGGER and r.exc_info]
    assert len(failures) == 1 and failures[0].levelno == logging.WARNING
    assert owner.get(f"/channels/{general}/permissions").json()["roles"] == [
        {"role_id": crew.everyone["id"], "allow": "0", "deny": str(VIEW)}]

    monkeypatch.undo()
    with listening(owner, owner=owner) as live:
        assert overwrite(owner, channel_rule(general, "roles", crew.mod_role["id"]),
                         deny=SEND).status_code == 200
        assert len(of_type(live.settle()["owner"], ROW_FRAME)) == 1


def test_a_rejected_write_queues_nothing(crew, caplog):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    for member_client in (crew.member_client, crew.mod_client):
        assert member_client.get(f"/servers/{sid}/channels").status_code == 200
    drain_jobs(owner)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), \
            listening(owner, owner=owner, mod=crew.mod_client) as live:
        res = put(crew.mod_client, channel_rule(general, "roles", crew.everyone["id"]), deny=VIEW)
        assert (res.status_code, res.json()["detail"]) == (403, LOSE_OWN_ACCESS)
        res = put(crew.mod_client, channel_rule(general, "members", crew.member["id"]),
                  allow=MANAGE_MESSAGES)
        assert (res.status_code, res.json()["detail"]) == (403, BITS_YOU_LACK)
        assert live.settle() == {"owner": [], "mod": []}
    assert job_query_counts(caplog) == []
    assert owner.get(f"/channels/{general}/permissions").json() == {"roles": [], "members": []}
    assert (crew.member["id"], sid) in permissions._channel_cache
    assert (crew.mod["id"], sid) in permissions._channel_cache


def test_the_target_deleted_before_the_job(crew, caplog):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER), \
            listening(owner, owner=owner, member=crew.member_client) as live:
        with held_queue(owner, sid) as release:
            res = put(owner, channel_rule(general, "roles", crew.everyone["id"]), deny=VIEW)
            assert res.status_code == 200
            assert owner.delete(f"/channels/{general}").status_code == 204
            release()
            frames = live.settle()
    assert not any(of_type(fs, ROW_FRAME) or of_type(fs, "channel_updated")
                   for fs in frames.values())
    assert [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING] == []


def test_a_member_who_leaves_before_the_job_gets_nothing(crew, new_client):
    owner, sid, general = crew.owner_client, crew.sid, crew.general["id"]
    stayer_client, _ = join(owner, new_client, sid)
    with listening(owner, owner=owner, stayer=stayer_client, leaver=crew.member_client) as live:
        with held_queue(owner, sid) as release:
            res = put(owner, channel_rule(general, "roles", crew.everyone["id"]), deny=VIEW)
            assert res.status_code == 200
            assert owner.delete(f"/servers/{sid}/members/{crew.member['id']}").status_code == 204
            release()
            frames = live.settle()
    assert [f["channel_id"] for f in of_type(frames["stayer"], "channel_deleted")] == [general]
    assert not of_type(frames["leaver"], "channel_deleted")


# -- what the job costs ---------------------------------------------------------------------

def test_job_queries_do_not_grow_with_members(client, new_client, caplog):
    register(client)
    last = []
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for name, fillers in (("Small", 5), ("Large", 59)):
            sid = create_server(client, name)["id"]
            general = create_channel(client, sid, "general")["id"]
            reader, _ = join(client, new_client, sid)
            first = post_message(client, sid, general, "one")
            post_message(client, sid, general, "two")
            assert reader.put(f"/channels/{general}/read", json={"message_id": first}).status_code == 200
            add_members(client, sid, fillers)
            path = channel_rule(general, "roles", roles_by_name(client, sid)["@everyone"]["id"])
            caplog.clear()
            assert overwrite(client, path, deny=VIEW).status_code == 200
            assert overwrite(client, path).status_code == 204
            last.append(job_query_counts(caplog)[-1])
    assert last[0] == last[1] <= 9


def test_job_queries_do_not_grow_with_channels_in_a_category(client, caplog):
    register(client)
    last = []
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for index, channels in enumerate((1, 6)):
            sid, group, everyone = category_server(client, f"Server {index}", channels, 3)
            path = group_rule(group, "roles", everyone)
            caplog.clear()
            assert overwrite(client, path, deny=VIEW).status_code == 200
            assert overwrite(client, path).status_code == 204
            last.append(job_query_counts(caplog)[-1])
    assert last[0] == last[1] <= 9


def test_members_in_voice_cost_no_queries(crew, monkeypatch, caplog):
    owner, sid = crew.owner_client, crew.sid
    occupied = create_channel(owner, sid, "room", "voice")["id"]
    empty = create_channel(owner, sid, "quiet", "voice")["id"]
    source = use_presence(owner, monkeypatch, {
        occupied: (VoiceParticipant(crew.member["id"], muted=False, deafened=False),)})

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert overwrite(owner, channel_rule(empty, "members", crew.member["id"]),
                         deny=CONNECT).status_code == 200
        assert source.removed == []
        assert overwrite(owner, channel_rule(occupied, "members", crew.member["id"]),
                         deny=CONNECT).status_code == 200
    assert source.removed == [(occupied, crew.member["id"])]
    without, with_voice = job_query_counts(caplog)[-2:]
    assert without == with_voice


# -- changes that fan out inline wait for the queued jobs ------------------------------

def test_a_role_change_waits_for_the_queued_fan_out(crew, monkeypatch):
    owner, sid, secret = crew.owner_client, crew.sid, crew.secret["id"]
    viewer = owner.post(f"/servers/{sid}/roles", json={"name": "Viewer"}).json()
    assert set_roles(owner, sid, crew.member["id"], [viewer["id"]]).status_code == 200
    path = channel_rule(secret, "roles", viewer["id"])
    assert overwrite(owner, path, allow=VIEW).status_code == 200
    original = overwrites_router.announce_overwrite

    async def late(*args, **kwargs):
        await asyncio.sleep(0.3)
        await original(*args, **kwargs)

    monkeypatch.setattr(overwrites_router, "announce_overwrite", late)
    with listening(owner, owner=owner, member=crew.member_client) as live:
        assert owner.delete(path).status_code == 204
        assert set_roles(owner, sid, crew.member["id"], []).status_code == 200
        frames = live.settle()
    assert [f["channel_id"] for f in of_type(frames["member"], "channel_deleted")] == [secret]


def test_a_role_deletion_waits_for_the_queued_fan_out(crew, monkeypatch):
    owner, sid, secret = crew.owner_client, crew.sid, crew.secret["id"]
    viewer = owner.post(f"/servers/{sid}/roles", json={"name": "Viewer"}).json()
    assert set_roles(owner, sid, crew.member["id"], [viewer["id"]]).status_code == 200
    path = channel_rule(secret, "roles", viewer["id"])
    assert overwrite(owner, path, allow=VIEW).status_code == 200
    original = overwrites_router.announce_overwrite

    async def late(*args, **kwargs):
        await asyncio.sleep(0.3)
        await original(*args, **kwargs)

    monkeypatch.setattr(overwrites_router, "announce_overwrite", late)
    with listening(owner, owner=owner, member=crew.member_client) as live:
        assert owner.delete(path).status_code == 204
        assert owner.delete(f"/servers/{sid}/roles/{viewer['id']}").status_code == 204
        frames = live.settle()
    assert [f["channel_id"] for f in of_type(frames["member"], "channel_deleted")] == [secret]


# -- a non-owner is checked against the category the channel is in now -------------------

def moving_out_of_its_category(monkeypatch, channel_id, delete=False):
    """Make the channel leave its category (or go away) right after the write
    gets the server lock, as a request that committed just before would."""
    real = channel_layout.locked_server

    @asynccontextmanager
    async def moving(server_id):
        async with real(server_id):
            if delete:
                await Channel.filter(id=channel_id).delete()
            else:
                await Channel.filter(id=channel_id).update(group_id=None)
            yield

    monkeypatch.setattr(overwrites_router.channel_layout, "locked_server", moving)


def test_a_non_owner_is_checked_against_the_category_the_channel_is_in_now(crew, monkeypatch):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "G"}).json()
    channel = owner.post(f"/channels/{sid}/create", json={
        "name": "x", "group_id": group["id"]}).json()["id"]
    assert overwrite(owner, group_rule(group["id"], "roles", crew.mod_role["id"]),
                     allow=MANAGE_MESSAGES).status_code == 200
    moving_out_of_its_category(monkeypatch, channel)

    res = put(crew.mod_client, channel_rule(channel, "roles", crew.everyone["id"]),
              allow=MANAGE_MESSAGES)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == BITS_YOU_LACK
    assert owner.get(f"/channels/{channel}/permissions").json() == {"roles": [], "members": []}


def test_a_channel_deleted_under_a_non_owner_write_is_not_found(crew, monkeypatch):
    owner, sid = crew.owner_client, crew.sid
    channel = create_channel(owner, sid, "gone")["id"]
    moving_out_of_its_category(monkeypatch, channel, delete=True)

    res = put(crew.mod_client, channel_rule(channel, "roles", crew.everyone["id"]), deny=SEND)
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Channel not found"
