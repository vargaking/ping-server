"""Channel categories: the snapshot, group CRUD, the layout endpoint and its frames."""
import pytest

from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_permissions import join, promote
from tests.test_realtime_events import chat_frame

HEADERS = {"origin": ORIGIN}


def snapshot(client, server_id):
    res = client.get(f"/servers/{server_id}/channels")
    assert res.status_code == 200, res.text
    return res.json()


def layout_of(client, server_id):
    data = snapshot(client, server_id)
    by_group = {}
    for channel in data["channels"]:
        by_group.setdefault(channel["group_id"], []).append(channel["id"])
    return {
        "ungrouped": by_group.get(None, []),
        "groups": [
            {"id": g["id"], "channel_ids": by_group.get(g["id"], [])} for g in data["groups"]],
    }


def new_group(client, server_id, name="Games"):
    res = client.post(f"/servers/{server_id}/channel-groups", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def team(client, new_client):
    register(client)
    server = create_server(client)
    member_client, _ = join(client, new_client, server["id"])
    admin_client, admin = join(client, new_client, server["id"])
    promote(client, server["id"], admin["id"])
    return {
        "sid": server["id"], "owner": client,
        "member": member_client, "admin": admin_client,
    }


# -- seeding and the snapshot ----------------------------------------------

def test_new_server_gets_the_two_default_groups(client):
    register(client)
    sid = create_server(client)["id"]

    groups = snapshot(client, sid)["groups"]

    assert [(g["name"], g["position"], g["server_id"]) for g in groups] == [
        ("Text channels", 0, sid), ("Voice channels", 1, sid)]


def test_snapshot_returns_groups_and_channels_in_layout_order(client):
    register(client)
    sid = create_server(client)["id"]
    text_group, voice_group = snapshot(client, sid)["groups"]
    a = create_channel(client, sid, "a")
    b = create_channel(client, sid, "b")
    c = create_channel(client, sid, "c", "voice")
    for channel, group in ((a, text_group), (c, voice_group), (b, text_group)):
        client.patch(f"/channels/{channel['id']}", json={"group_id": group["id"]})
    loose = create_channel(client, sid, "loose")

    data = snapshot(client, sid)

    assert set(data) == {"groups", "channels"}
    assert [c["id"] for c in data["channels"]] == [loose["id"], a["id"], b["id"], c["id"]]
    assert [(c["group_id"], c["position"]) for c in data["channels"]] == [
        (None, 0), (text_group["id"], 0), (text_group["id"], 1), (voice_group["id"], 0)]
    assert {"id", "name", "type", "topic", "channel_settings",
            "last_read_message_id", "last_message_id"} <= set(data["channels"][0])


def test_plain_channel_list_stays_a_list_with_the_new_fields(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]
    channel = create_channel(client, sid, "a")
    client.patch(f"/channels/{channel['id']}", json={"group_id": group["id"]})

    res = client.get(f"/channels/{sid}").json()

    assert isinstance(res, list)
    assert (res[0]["group_id"], res[0]["position"]) == (group["id"], 0)


def test_snapshot_is_hidden_from_non_members(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    outsider = new_client()
    register(outsider)

    assert outsider.get(f"/servers/{sid}/channels").status_code == 404
    assert outsider.post(f"/servers/{sid}/channel-groups", json={"name": "x"}).status_code == 404


# -- group CRUD ---------------------------------------------------------------

def test_create_group_appends_and_trims_the_name(client):
    register(client)
    sid = create_server(client)["id"]

    group = new_group(client, sid, "  Games  ")

    assert (group["name"], group["position"], group["server_id"]) == ("Games", 2, sid)
    assert [g["id"] for g in snapshot(client, sid)["groups"]][-1] == group["id"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_group_name_is_validated(client, name):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]

    assert client.post(f"/servers/{sid}/channel-groups", json={"name": name}).status_code == 422
    res = client.patch(f"/servers/{sid}/channel-groups/{group['id']}", json={"name": name})
    assert res.status_code == 422


def test_rename_group(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]

    res = client.patch(f"/servers/{sid}/channel-groups/{group['id']}", json={"name": "Chat"})

    assert res.status_code == 200
    assert res.json() == {**group, "name": "Chat"}
    assert snapshot(client, sid)["groups"][0]["name"] == "Chat"


def test_group_of_another_server_is_not_found(client):
    register(client)
    sid = create_server(client)["id"]
    other_group = snapshot(client, create_server(client, "Other")["id"])["groups"][0]

    assert client.patch(
        f"/servers/{sid}/channel-groups/{other_group['id']}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/servers/{sid}/channel-groups/{other_group['id']}").status_code == 404
    assert client.delete(f"/servers/{sid}/channel-groups/9999").status_code == 404
    assert len(snapshot(client, sid)["groups"]) == 2


def test_group_count_is_capped(client, monkeypatch):
    from app.services import channel_layout
    monkeypatch.setattr(channel_layout, "MAX_GROUPS", 3)
    register(client)
    sid = create_server(client)["id"]
    new_group(client, sid)

    res = client.post(f"/servers/{sid}/channel-groups", json={"name": "one too many"})

    assert res.status_code == 422


def test_delete_group_keeps_channels_and_messages_and_appends_them_to_ungrouped(client):
    register(client)
    sid = create_server(client)["id"]
    text_group, voice_group = snapshot(client, sid)["groups"]
    loose = create_channel(client, sid, "loose")
    a = create_channel(client, sid, "a")
    b = create_channel(client, sid, "b")
    kept = create_channel(client, sid, "kept", "voice")
    for channel in (b, a):
        client.patch(f"/channels/{channel['id']}", json={"group_id": text_group["id"]})
    client.patch(f"/channels/{kept['id']}", json={"group_id": voice_group["id"]})
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(chat_frame(sid, a["id"]))
        assert ws.receive_json()["type"] == "message_ack"

    res = client.delete(f"/servers/{sid}/channel-groups/{text_group['id']}")

    assert res.status_code == 204
    assert layout_of(client, sid) == {
        "ungrouped": [loose["id"], b["id"], a["id"]],
        "groups": [{"id": voice_group["id"], "channel_ids": [kept["id"]]}],
    }
    assert len(client.get(f"/channels/{a['id']}/messages").json()["messages"]) == 1


def test_deleting_a_server_removes_its_groups(client):
    from app.models.ChannelGroup import ChannelGroup
    register(client)
    sid = create_server(client)["id"]
    assert client.delete(f"/servers/{sid}").status_code == 204

    assert client.portal.call(ChannelGroup.filter(server_id=sid).count) == 0


def test_deleting_a_group_row_leaves_its_channels_ungrouped(client):
    from app.models.ChannelGroup import ChannelGroup
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]
    channel = create_channel(client, sid, "a")
    client.patch(f"/channels/{channel['id']}", json={"group_id": group["id"]})

    client.portal.call(ChannelGroup.filter(id=group["id"]).delete)

    assert layout_of(client, sid)["ungrouped"] == [channel["id"]]


# -- permissions --------------------------------------------------------------

def test_plain_member_cannot_write_categories_or_layout(team):
    sid, member, owner = team["sid"], team["member"], team["owner"]
    group = snapshot(owner, sid)["groups"][0]
    channel = create_channel(owner, sid, "a")
    layout = layout_of(owner, sid)

    assert member.post(f"/servers/{sid}/channel-groups", json={"name": "x"}).status_code == 403
    assert member.patch(
        f"/servers/{sid}/channel-groups/{group['id']}", json={"name": "x"}).status_code == 403
    assert member.delete(f"/servers/{sid}/channel-groups/{group['id']}").status_code == 403
    assert member.put(f"/servers/{sid}/channel-layout", json=layout).status_code == 403
    assert member.post(
        f"/channels/{sid}/create", json={"name": "b", "group_id": group["id"]}).status_code == 403
    assert member.patch(
        f"/channels/{channel['id']}", json={"group_id": group["id"]}).status_code == 403

    assert snapshot(member, sid)["groups"][0]["name"] == group["name"]
    assert layout_of(owner, sid) == layout


def test_members_can_read_and_admins_can_write(team):
    sid = team["sid"]

    assert team["member"].get(f"/servers/{sid}/channels").status_code == 200
    assert team["admin"].post(
        f"/servers/{sid}/channel-groups", json={"name": "Admin made"}).status_code == 201


# -- layout -------------------------------------------------------------------

@pytest.fixture
def laid_out(client):
    register(client)
    sid = create_server(client)["id"]
    text_group, voice_group = snapshot(client, sid)["groups"]
    channels = [create_channel(client, sid, f"c{i}") for i in range(4)]
    return {
        "sid": sid, "text": text_group["id"], "voice": voice_group["id"],
        "ids": [c["id"] for c in channels],
    }


def test_layout_renumbers_and_returns_the_normalized_layout(client, laid_out):
    sid, text, voice = laid_out["sid"], laid_out["text"], laid_out["voice"]
    a, b, c, d = laid_out["ids"]
    body = {
        "ungrouped": [d],
        "groups": [{"id": voice, "channel_ids": [b]}, {"id": text, "channel_ids": [c, a]}],
    }

    res = client.put(f"/servers/{sid}/channel-layout", json=body)

    assert res.status_code == 200, res.text
    assert res.json() == body
    data = snapshot(client, sid)
    assert [g["id"] for g in data["groups"]] == [voice, text]
    assert [g["position"] for g in data["groups"]] == [0, 1]
    assert [(ch["id"], ch["group_id"], ch["position"]) for ch in data["channels"]] == [
        (d, None, 0), (b, voice, 0), (c, text, 0), (a, text, 1)]
    assert client.get(f"/servers/{sid}").json()["server_settings"]["channel_order"] == [d, b, c, a]


def test_layout_allows_empty_groups_and_no_channels(client):
    register(client)
    sid = create_server(client)["id"]
    text, voice = (g["id"] for g in snapshot(client, sid)["groups"])

    res = client.put(f"/servers/{sid}/channel-layout", json={
        "ungrouped": [], "groups": [{"id": voice, "channel_ids": []}, {"id": text, "channel_ids": []}]})

    assert res.status_code == 200
    assert [g["id"] for g in snapshot(client, sid)["groups"]] == [voice, text]


def test_layout_rejects_anything_but_exactly_the_servers_channels_and_groups(client, laid_out):
    sid, text, voice = laid_out["sid"], laid_out["text"], laid_out["voice"]
    a, b, c, d = laid_out["ids"]
    foreign_sid = create_server(client, "Other")
    foreign_channel = create_channel(client, foreign_sid["id"], "x")["id"]
    foreign_group = snapshot(client, foreign_sid["id"])["groups"][0]["id"]
    before = layout_of(client, sid)

    def groups(first=None, second=None, voice_id=voice, text_id=text):
        return [{"id": text_id, "channel_ids": first or [a, b]},
                {"id": voice_id, "channel_ids": second or [c]}]

    cases = {
        "missing channel": ({"ungrouped": [], "groups": groups()}, "missing channel ids"),
        "duplicate channel": ({"ungrouped": [d, a], "groups": groups()}, "duplicated channel ids"),
        "foreign channel": (
            {"ungrouped": [d, foreign_channel], "groups": groups()}, "unknown channel ids"),
        "unknown channel": ({"ungrouped": [d, 99999], "groups": groups()}, "unknown channel ids"),
        "missing group": ({"ungrouped": [d], "groups": groups()[:1]}, "missing group ids"),
        "duplicate group": (
            {"ungrouped": [d], "groups": groups() + [{"id": text, "channel_ids": []}]},
            "duplicated group ids"),
        "foreign group": (
            {"ungrouped": [d], "groups": groups(voice_id=foreign_group)}, "unknown group ids"),
    }
    for label, (body, expected) in cases.items():
        res = client.put(f"/servers/{sid}/channel-layout", json=body)
        assert res.status_code == 422, (label, res.text)
        assert expected in res.json()["detail"], (label, res.json())
    assert layout_of(client, sid) == before


@pytest.mark.parametrize("bad", ["1", True, 1.5, None])
def test_layout_ids_must_be_strict_ints(client, laid_out, bad):
    sid, text, voice = laid_out["sid"], laid_out["text"], laid_out["voice"]
    a, b, c, d = laid_out["ids"]

    in_channels = {"ungrouped": [bad, b, c, d], "groups": [
        {"id": text, "channel_ids": []}, {"id": voice, "channel_ids": []}]}
    in_groups = {"ungrouped": [a, b, c, d], "groups": [
        {"id": bad, "channel_ids": []}, {"id": voice, "channel_ids": []}]}

    assert client.put(f"/servers/{sid}/channel-layout", json=in_channels).status_code == 422
    assert client.put(f"/servers/{sid}/channel-layout", json=in_groups).status_code == 422


def test_layout_lists_are_bounded(client, laid_out):
    sid = laid_out["sid"]

    res = client.put(f"/servers/{sid}/channel-layout", json={
        "ungrouped": list(range(501)), "groups": []})

    assert res.status_code == 422


def test_channel_count_is_capped(client, monkeypatch):
    from app.services import channel_layout
    monkeypatch.setattr(channel_layout, "MAX_CHANNELS", 2)
    register(client)
    sid = create_server(client)["id"]
    create_channel(client, sid, "a")
    create_channel(client, sid, "b")

    res = client.post(f"/channels/{sid}/create", json={"name": "c"})

    assert res.status_code == 422


# -- channel create / update with group_id ------------------------------------

def test_create_channel_in_a_group_appends_to_it(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]["id"]

    first = client.post(f"/channels/{sid}/create", json={"name": "a", "group_id": group}).json()
    second = client.post(f"/channels/{sid}/create", json={"name": "b", "group_id": group}).json()
    loose = client.post(f"/channels/{sid}/create", json={"name": "c", "group_id": None}).json()

    assert [(c["group_id"], c["position"]) for c in (first, second, loose)] == [
        (group, 0), (group, 1), (None, 0)]


def test_create_channel_rejects_unknown_and_foreign_groups(client):
    register(client)
    sid = create_server(client)["id"]
    foreign = snapshot(client, create_server(client, "Other")["id"])["groups"][0]["id"]

    for bad in (foreign, 9999, "1", True):
        res = client.post(f"/channels/{sid}/create", json={"name": "a", "group_id": bad})
        assert res.status_code == 422, (bad, res.text)
    assert snapshot(client, sid)["channels"] == []


def test_move_channel_between_groups_and_to_ungrouped(client):
    register(client)
    sid = create_server(client)["id"]
    text, voice = (g["id"] for g in snapshot(client, sid)["groups"])
    a = create_channel(client, sid, "a")
    b = create_channel(client, sid, "b")
    client.patch(f"/channels/{a['id']}", json={"group_id": text})
    res = client.patch(f"/channels/{b['id']}", json={"group_id": text})
    assert (res.json()["group_id"], res.json()["position"]) == (text, 1)

    res = client.patch(f"/channels/{a['id']}", json={"group_id": voice})
    assert (res.json()["group_id"], res.json()["position"]) == (voice, 0)
    res = client.patch(f"/channels/{a['id']}", json={"group_id": None})
    assert (res.json()["group_id"], res.json()["position"]) == (None, 0)
    assert layout_of(client, sid) == {
        "ungrouped": [a["id"]],
        "groups": [{"id": text, "channel_ids": [b["id"]]}, {"id": voice, "channel_ids": []}],
    }


def test_update_without_group_id_or_with_the_same_group_keeps_the_position(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]["id"]
    a = client.post(f"/channels/{sid}/create", json={"name": "a", "group_id": group}).json()
    client.post(f"/channels/{sid}/create", json={"name": "b", "group_id": group})

    renamed = client.patch(f"/channels/{a['id']}", json={"name": "renamed"}).json()
    same = client.patch(f"/channels/{a['id']}", json={"group_id": group}).json()

    assert (renamed["group_id"], renamed["position"]) == (group, 0)
    assert (same["group_id"], same["position"]) == (group, 0)


def test_update_rejects_unknown_foreign_and_non_int_groups(client):
    register(client)
    sid = create_server(client)["id"]
    channel = create_channel(client, sid, "a")
    foreign = snapshot(client, create_server(client, "Other")["id"])["groups"][0]["id"]

    for bad in (foreign, 9999, "1", True):
        res = client.patch(f"/channels/{channel['id']}", json={"group_id": bad})
        assert res.status_code == 422, (bad, res.text)
    assert layout_of(client, sid)["ungrouped"] == [channel["id"]]


def test_deleting_a_channel_leaves_the_rest_of_the_layout(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]["id"]
    a = client.post(f"/channels/{sid}/create", json={"name": "a", "group_id": group}).json()
    b = client.post(f"/channels/{sid}/create", json={"name": "b", "group_id": group}).json()

    client.delete(f"/channels/{a['id']}")

    assert snapshot(client, sid)["channels"][0]["id"] == b["id"]
    assert layout_of(client, sid)["groups"][0] == {"id": group, "channel_ids": [b["id"]]}


# -- channel_order compatibility -----------------------------------------------

def test_channel_order_alone_is_accepted_and_ignored(team):
    sid, owner, admin = team["sid"], team["owner"], team["admin"]
    a = create_channel(owner, sid, "a")
    b = create_channel(owner, sid, "b")
    before = owner.get(f"/servers/{sid}").json()
    assert before["server_settings"]["channel_order"] == [a["id"], b["id"]]

    res = admin.put(f"/servers/{sid}", json={"server_settings": {"channel_order": [b["id"], a["id"]]}})

    assert res.status_code == 200, res.text
    assert owner.get(f"/servers/{sid}").json()["server_settings"] == before["server_settings"]
    assert layout_of(owner, sid)["ungrouped"] == [a["id"], b["id"]]


def test_channel_order_is_dropped_when_sent_with_other_fields(client):
    register(client)
    sid = create_server(client)["id"]
    a = create_channel(client, sid, "a")
    b = create_channel(client, sid, "b")

    res = client.put(f"/servers/{sid}", json={
        "name": "Renamed",
        "server_settings": {"channel_order": [b["id"], a["id"]], "default_channel_id": a["id"]},
    })

    assert res.status_code == 200, res.text
    fetched = client.get(f"/servers/{sid}").json()
    assert fetched["name"] == "Renamed"
    assert fetched["server_settings"] == {
        "default_channel_id": a["id"], "channel_order": [a["id"], b["id"]]}


def test_derived_channel_order_is_in_every_server_response(client):
    register(client)
    sid = create_server(client)["id"]
    group = snapshot(client, sid)["groups"][0]["id"]
    a = create_channel(client, sid, "a")
    b = client.post(f"/channels/{sid}/create", json={"name": "b", "group_id": group}).json()

    mine = next(s for s in client.get("/servers/me").json() if s["id"] == sid)
    assert mine["server_settings"]["channel_order"] == [a["id"], b["id"]]
    assert client.get(f"/servers/{sid}").json()["server_settings"]["channel_order"] == [
        a["id"], b["id"]]
    fresh = create_server(client, "Fresh")
    assert fresh["server_settings"]["channel_order"] == []


def test_channel_order_is_never_stored(client):
    from app.models.Server import Server
    register(client)
    sid = create_server(client)["id"]
    create_channel(client, sid, "a")
    client.put(f"/servers/{sid}", json={"name": "N", "server_settings": {"channel_order": [1]}})

    async def stored_settings():
        return (await Server.get(id=sid)).server_settings

    stored = client.portal.call(stored_settings)

    assert "channel_order" not in stored


# -- websocket frames ---------------------------------------------------------

@pytest.fixture
def sockets(client, team):
    """The owner's and a member's sockets, both ready."""
    with client.websocket_connect("/ws", headers=HEADERS) as owner_ws, \
            team["member"].websocket_connect("/ws", headers=HEADERS) as member_ws:
        ws_ready(owner_ws)
        ws_ready(member_ws)
        owner_ws.receive_json()  # member online
        yield owner_ws, member_ws


def assert_actor_excluded(sid, owner_ws, member_ws, channel_id):
    """The owner's next frame is the member's message, not the frame she caused."""
    member_ws.send_json(chat_frame(sid, channel_id))
    assert owner_ws.receive_json()["type"] == "message"


def test_group_frames_reach_other_members_and_not_the_actor(client, team, sockets):
    sid = team["sid"]
    owner_ws, member_ws = sockets
    channel = create_channel(client, sid, "a")
    member_ws.receive_json()  # channel_created

    group = new_group(client, sid, "Games")
    assert member_ws.receive_json() == {
        "type": "channel_group_created", "server_id": sid, "group": group}

    res = client.patch(f"/servers/{sid}/channel-groups/{group['id']}", json={"name": "Play"})
    assert member_ws.receive_json() == {
        "type": "channel_group_updated", "server_id": sid, "group": res.json()}

    client.patch(f"/channels/{channel['id']}", json={"group_id": group["id"]})
    frame = member_ws.receive_json()
    assert frame["type"] == "channel_updated"
    assert (frame["channel"]["group_id"], frame["channel"]["position"]) == (group["id"], 0)

    client.delete(f"/servers/{sid}/channel-groups/{group['id']}")
    assert member_ws.receive_json() == {
        "type": "channel_group_deleted", "server_id": sid, "group_id": group["id"],
        "layout": {
            "ungrouped": [channel["id"]],
            "groups": [{"id": g["id"], "channel_ids": []} for g in snapshot(client, sid)["groups"]],
        },
    }
    assert_actor_excluded(sid, owner_ws, member_ws, channel["id"])


def test_layout_frame_reaches_other_members_and_not_the_actor(client, team, sockets):
    sid = team["sid"]
    owner_ws, member_ws = sockets
    text, voice = (g["id"] for g in snapshot(client, sid)["groups"])
    a = create_channel(client, sid, "a")
    b = create_channel(client, sid, "b")
    member_ws.receive_json()
    member_ws.receive_json()
    body = {"ungrouped": [], "groups": [
        {"id": voice, "channel_ids": [a["id"]]}, {"id": text, "channel_ids": [b["id"]]}]}

    res = client.put(f"/servers/{sid}/channel-layout", json=body)

    assert res.status_code == 200, res.text
    assert member_ws.receive_json() == {
        "type": "channel_layout_updated", "server_id": sid, "layout": body}
    assert_actor_excluded(sid, owner_ws, member_ws, a["id"])


def test_channel_created_frame_carries_group_and_position(client, team, sockets):
    sid = team["sid"]
    owner_ws, member_ws = sockets
    group = snapshot(client, sid)["groups"][0]["id"]

    channel = client.post(f"/channels/{sid}/create", json={"name": "a", "group_id": group}).json()

    frame = member_ws.receive_json()
    assert frame["type"] == "channel_created"
    assert (frame["channel"]["group_id"], frame["channel"]["position"]) == (group, 0)
    assert_actor_excluded(sid, owner_ws, member_ws, channel["id"])


def test_failed_layout_sends_no_frame(client, team, sockets):
    sid = team["sid"]
    owner_ws, member_ws = sockets
    channel = create_channel(client, sid, "a")
    member_ws.receive_json()

    res = client.put(f"/servers/{sid}/channel-layout", json={"ungrouped": [], "groups": []})

    assert res.status_code == 422
    assert_actor_excluded(sid, owner_ws, member_ws, channel["id"])
