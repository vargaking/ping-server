"""Custom roles: the role endpoints, the rank and bit rules, realtime and voice."""
import pytest

from app.permissions import ALL_PERMISSIONS, MEMBER_PERMISSIONS, Permission
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_permissions import join, roles_by_name, set_roles
from tests.test_voice_moderation import _in_voice, use_presence

HEADERS = {"origin": ORIGIN}

SEND = Permission.SEND_MESSAGES
KICK = Permission.KICK_MEMBERS
MANAGE_MESSAGES = Permission.MANAGE_MESSAGES
MANAGE_ROLES = Permission.MANAGE_ROLES
MANAGE_SERVER = Permission.MANAGE_SERVER


def bits(*perms) -> str:
    value = 0
    for perm in perms:
        value |= int(perm)
    return str(value)


def create_role(client, sid, name, **fields):
    res = client.post(f"/servers/{sid}/roles", json={"name": name, **fields})
    assert res.status_code == 201, res.text
    return res.json()


def patch_role(client, sid, role_id, **fields):
    return client.patch(f"/servers/{sid}/roles/{role_id}", json=fields)


def order_roles(client, sid, role_ids):
    return client.put(f"/servers/{sid}/roles/order", json={"role_ids": role_ids})


def ranking(client, sid):
    """Role names from the top of the list down."""
    return [r["name"] for r in client.get(f"/servers/{sid}/roles").json()]


def mask_of(client, sid) -> int:
    return int(client.get(f"/servers/{sid}").json()["permissions"])


@pytest.fixture
def crew(client, new_client):
    """The owner (the `client`), B holding a Mod role that can manage roles and
    kick, and two plain members."""
    owner = register(client)
    server = create_server(client)
    sid = server["id"]
    lounge = create_channel(client, sid, "Lounge", "voice")
    b_client, b = join(client, new_client, sid)
    member_client, member = join(client, new_client, sid)
    other_client, other = join(client, new_client, sid)
    mod = create_role(client, sid, "Mod", allow=bits(KICK, MANAGE_ROLES))
    assert set_roles(client, sid, b["id"], [mod["id"]]).status_code == 200
    return {
        "owner": owner, "sid": sid, "lounge": lounge, "mod": mod,
        "b_client": b_client, "b": b,
        "member_client": member_client, "member": member,
        "other_client": other_client, "other": other,
    }


# -- create --------------------------------------------------------------

def test_new_role_lands_at_position_one_and_the_others_shift_up(client):
    register(client)
    sid = create_server(client)["id"]

    first = create_role(client, sid, "Mod")
    second = create_role(client, sid, "  Helper  ", color="#A1b2C3")

    assert (first["position"], second["position"]) == (1, 1)
    assert second["name"] == "Helper"
    assert second["color"] == "#A1b2C3"
    assert (first["allow"], first["deny"], first["parent_id"], first["is_default"]) == (
        "0", "0", None, False)
    listed = client.get(f"/servers/{sid}/roles").json()
    assert [(r["name"], r["position"]) for r in listed] == [
        ("Admin", 3), ("Mod", 2), ("Helper", 1), ("@everyone", 0)]


def test_create_accepts_initial_fields(client):
    register(client)
    sid = create_server(client)["id"]
    parent = create_role(client, sid, "Parent", allow=bits(KICK))

    child = create_role(
        client, sid, "Child", allow=bits(MANAGE_MESSAGES), deny=bits(SEND),
        parent_id=parent["id"], color=None)

    assert (child["allow"], child["deny"], child["parent_id"]) == (
        bits(MANAGE_MESSAGES), bits(SEND), parent["id"])


def test_create_is_validated(client):
    register(client)
    sid = create_server(client)["id"]
    create_role(client, sid, "Taken")
    url = f"/servers/{sid}/roles"

    assert client.post(url, json={"name": "Taken"}).status_code == 409
    for body in (
        {}, {"name": "   "}, {"name": "x" * 51}, {"name": "a", "color": "red"},
        {"name": "a", "color": "#12345"}, {"name": "a", "allow": "abc"},
        {"name": "a", "allow": str(1 << 40)}, {"name": "a", "deny": -1},
        {"name": "a", "allow": bits(KICK), "deny": bits(KICK)},
        {"name": "a", "parent_id": 999999}, {"name": "a", "allow": True},
        {"name": "a", "allow": None},
    ):
        assert client.post(url, json=body).status_code == 422, body


def test_create_accepts_integer_permissions_too(client):
    register(client)
    sid = create_server(client)["id"]
    role = client.post(f"/servers/{sid}/roles", json={"name": "Ints", "allow": int(KICK)})
    assert role.status_code == 201
    assert role.json()["allow"] == bits(KICK)


def test_a_server_holds_at_most_one_hundred_roles(client):
    register(client)
    sid = create_server(client)["id"]
    for index in range(98):
        create_role(client, sid, f"r{index}")

    assert len(client.get(f"/servers/{sid}/roles").json()) == 100
    assert client.post(f"/servers/{sid}/roles", json={"name": "one too many"}).status_code == 422


def test_parent_from_another_server_is_rejected(client):
    register(client)
    sid = create_server(client, "One")["id"]
    elsewhere = create_role(client, create_server(client, "Two")["id"], "Foreign")

    res = client.post(f"/servers/{sid}/roles", json={"name": "a", "parent_id": elsewhere["id"]})
    assert res.status_code == 422
    assert client.get(f"/servers/{sid}/roles").json()[-1]["name"] == "@everyone"
    assert len(client.get(f"/servers/{sid}/roles").json()) == 2


def test_roles_need_manage_roles(crew):
    sid = crew["sid"]
    member = crew["member_client"]
    role = create_role(crew["b_client"], sid, "Mine")

    assert member.post(f"/servers/{sid}/roles", json={"name": "x"}).status_code == 403
    assert patch_role(member, sid, role["id"], name="y").status_code == 403
    assert member.delete(f"/servers/{sid}/roles/{role['id']}").status_code == 403
    assert order_roles(member, sid, []).status_code == 403


# -- patch ---------------------------------------------------------------

def test_patch_changes_each_field(client):
    register(client)
    sid = create_server(client)["id"]
    parent = create_role(client, sid, "Parent")
    role = create_role(client, sid, "Role")
    url = f"/servers/{sid}/roles/{role['id']}"

    assert client.patch(url, json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert client.patch(url, json={"color": "#00ff00"}).json()["color"] == "#00ff00"
    assert client.patch(url, json={"color": None}).json()["color"] is None
    assert client.patch(url, json={"allow": bits(KICK)}).json()["allow"] == bits(KICK)
    changed = client.patch(url, json={"allow": "0", "deny": bits(SEND)}).json()
    assert (changed["allow"], changed["deny"]) == ("0", bits(SEND))
    assert client.patch(url, json={"parent_id": parent["id"]}).json()["parent_id"] == parent["id"]
    assert client.patch(url, json={"parent_id": None}).json()["parent_id"] is None
    kept = client.patch(url, json={"name": "Again"}).json()
    assert (kept["deny"], kept["name"]) == (bits(SEND), "Again")


def test_patch_validation(client):
    register(client)
    sid = create_server(client)["id"]
    a = create_role(client, sid, "A")
    b = create_role(client, sid, "B", allow=bits(KICK))
    url = f"/servers/{sid}/roles/{b['id']}"

    assert client.patch(url, json={"name": "A"}).status_code == 409
    assert client.patch(url, json={"name": "B"}).status_code == 200
    assert client.patch(url, json={"color": "#xyzxyz"}).status_code == 422
    assert client.patch(url, json={"name": None}).status_code == 422
    assert client.patch(url, json={"deny": bits(KICK)}).status_code == 422
    assert client.patch(url, json={"allow": str(1 << 50)}).status_code == 422
    assert client.patch(url, json={"parent_id": b["id"]}).status_code == 422
    assert client.patch(url, json={"parent_id": 999999}).status_code == 422
    assert client.patch(f"/servers/{sid}/roles/999999", json={"name": "x"}).status_code == 404
    other = create_role(client, create_server(client, "Two")["id"], "Foreign")
    assert client.patch(
        f"/servers/{sid}/roles/{other['id']}", json={"name": "x"}).status_code == 404
    assert client.patch(url, json={"parent_id": a["id"]}).status_code == 200


def test_a_cycle_is_rejected_with_a_message(client):
    register(client)
    sid = create_server(client)["id"]
    a = create_role(client, sid, "A")
    b = create_role(client, sid, "B", parent_id=a["id"])
    c = create_role(client, sid, "C", parent_id=b["id"])

    res = patch_role(client, sid, a["id"], parent_id=c["id"])
    assert res.status_code == 422
    assert res.json()["detail"] == "That would make A inherit from itself"
    assert patch_role(client, sid, a["id"], parent_id=a["id"]).status_code == 422


def test_everyone_rules(client):
    register(client)
    sid = create_server(client)["id"]
    everyone = roles_by_name(client, sid)["@everyone"]
    admin = roles_by_name(client, sid)["Admin"]
    url = f"/servers/{sid}/roles/{everyone['id']}"

    assert patch_role(client, sid, everyone["id"], name="everybody").status_code == 422
    assert patch_role(client, sid, everyone["id"], parent_id=admin["id"]).status_code == 422
    assert patch_role(client, sid, everyone["id"], deny=bits(SEND)).status_code == 422
    assert client.delete(url).status_code == 422
    assert order_roles(client, sid, [admin["id"], everyone["id"]]).status_code == 422
    assert patch_role(client, sid, everyone["id"], name="@everyone", deny="0").status_code == 200
    assert patch_role(client, sid, admin["id"], parent_id=everyone["id"]).status_code == 200
    assert roles_by_name(client, sid)["@everyone"]["position"] == 0


def test_editing_everyone_changes_every_members_mask(crew):
    sid = crew["sid"]
    everyone = roles_by_name(crew["b_client"], sid)["@everyone"]
    assert mask_of(crew["member_client"], sid) == int(MEMBER_PERMISSIONS)

    res = patch_role(
        crew["b_client"], sid, everyone["id"], allow=bits(MEMBER_PERMISSIONS & ~SEND))
    assert res.status_code == 200, res.text

    for who in ("member_client", "other_client", "b_client"):
        assert not mask_of(crew[who], sid) & SEND


def test_admin_is_an_ordinary_role(client):
    register(client)
    sid = create_server(client)["id"]
    admin = roles_by_name(client, sid)["Admin"]

    assert patch_role(client, sid, admin["id"], name="Boss", allow=bits(KICK)).status_code == 200
    assert client.delete(f"/servers/{sid}/roles/{admin['id']}").status_code == 204
    assert ranking(client, sid) == ["@everyone"]


# -- delete and reorder --------------------------------------------------

def test_deleting_a_parent_leaves_children_with_their_own_bits(client, new_client):
    owner = register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    parent = create_role(client, sid, "Parent", allow=bits(KICK))
    child = create_role(client, sid, "Child", allow=bits(MANAGE_MESSAGES), parent_id=parent["id"])
    assert set_roles(client, sid, user["id"], [parent["id"], child["id"]]).status_code == 200
    assert mask_of(joiner, sid) & KICK

    assert client.delete(f"/servers/{sid}/roles/{parent['id']}").status_code == 204

    child_now = roles_by_name(client, sid)["Child"]
    assert child_now["parent_id"] is None
    assert child_now["position"] == 1
    mask = mask_of(joiner, sid)
    assert mask & MANAGE_MESSAGES and not mask & KICK
    members = {m["user"]["id"]: m for m in client.get(f"/servers/{sid}/members").json()}
    assert members[user["id"]]["role_ids"] == [child["id"]]
    assert owner["id"] in members
    assert ranking(client, sid) == ["Admin", "Child", "@everyone"]
    assert client.delete(f"/servers/{sid}/roles/{parent['id']}").status_code == 404


def test_deleting_a_role_removes_its_assignments(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    role = create_role(client, sid, "Temp", allow=bits(KICK))
    set_roles(client, sid, user["id"], [role["id"]])

    assert client.delete(f"/servers/{sid}/roles/{role['id']}").status_code == 204

    members = {m["user"]["id"]: m for m in client.get(f"/servers/{sid}/members").json()}
    assert members[user["id"]]["role_ids"] == []
    assert mask_of(joiner, sid) == int(MEMBER_PERMISSIONS)


def test_reorder_sets_positions_top_first(client):
    register(client)
    sid = create_server(client)["id"]
    a = create_role(client, sid, "A")
    b = create_role(client, sid, "B")
    admin = roles_by_name(client, sid)["Admin"]
    assert ranking(client, sid) == ["Admin", "A", "B", "@everyone"]

    res = order_roles(client, sid, [b["id"], admin["id"], a["id"]])

    assert res.status_code == 200, res.text
    assert [(r["name"], r["position"]) for r in res.json()] == [
        ("B", 3), ("Admin", 2), ("A", 1), ("@everyone", 0)]
    assert ranking(client, sid) == ["B", "Admin", "A", "@everyone"]


def test_reorder_needs_every_non_default_role_exactly_once(client):
    register(client)
    sid = create_server(client)["id"]
    a = create_role(client, sid, "A")
    admin = roles_by_name(client, sid)["Admin"]
    foreign = create_role(client, create_server(client, "Two")["id"], "Foreign")

    for ids in ([], [a["id"]], [a["id"], a["id"]], [a["id"], admin["id"], a["id"]],
                [a["id"], admin["id"], foreign["id"]], [a["id"], admin["id"], 999999]):
        assert order_roles(client, sid, ids).status_code == 422, ids
    assert client.put(f"/servers/{sid}/roles/order", json={}).status_code == 422
    assert ranking(client, sid) == ["Admin", "A", "@everyone"]


# -- resolution through the API -----------------

def test_a_role_grants_its_bits(crew, client):
    sid = crew["sid"]
    role = create_role(client, sid, "Helper", allow=bits(KICK, MANAGE_MESSAGES))
    set_roles(client, sid, crew["member"]["id"], [role["id"]])

    assert mask_of(crew["member_client"], sid) == int(MEMBER_PERMISSIONS | KICK | MANAGE_MESSAGES)
    assert mask_of(crew["other_client"], sid) == int(MEMBER_PERMISSIONS)


def test_a_child_inherits_and_its_deny_wins(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    mod = create_role(client, sid, "Mod", allow=bits(KICK, MANAGE_MESSAGES))
    trial = create_role(client, sid, "Trial mod", parent_id=mod["id"], deny=bits(KICK))
    set_roles(client, sid, user["id"], [trial["id"]])

    mask = mask_of(joiner, sid)
    assert mask & MANAGE_MESSAGES
    assert not mask & KICK


def test_muted_above_and_below_a_role_that_allows_the_bit(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    mod = create_role(client, sid, "Mod", allow=bits(SEND))
    muted = create_role(client, sid, "Muted", deny=bits(SEND))
    set_roles(client, sid, user["id"], [mod["id"], muted["id"]])
    assert ranking(client, sid) == ["Admin", "Mod", "Muted", "@everyone"]
    assert mask_of(joiner, sid) & SEND

    order_roles(client, sid, [muted["id"], mod["id"], roles_by_name(client, sid)["Admin"]["id"]])
    assert not mask_of(joiner, sid) & SEND

    order_roles(client, sid, [mod["id"], muted["id"], roles_by_name(client, sid)["Admin"]["id"]])
    assert mask_of(joiner, sid) & SEND


def test_two_unrelated_roles_add_up(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    kicker = create_role(client, sid, "Kicker", allow=bits(KICK))
    cleaner = create_role(client, sid, "Cleaner", allow=bits(MANAGE_MESSAGES))
    set_roles(client, sid, user["id"], [kicker["id"], cleaner["id"]])

    assert mask_of(joiner, sid) == int(MEMBER_PERMISSIONS | KICK | MANAGE_MESSAGES)


# -- rank and bit rules ---------------------------------------------------

def test_a_member_below_a_role_cannot_touch_it(crew):
    sid, b = crew["sid"], crew["b_client"]
    admin = roles_by_name(b, sid)["Admin"]
    mod = crew["mod"]

    for role in (admin, mod):
        assert patch_role(b, sid, role["id"], name="Nope").status_code == 403
        assert b.delete(f"/servers/{sid}/roles/{role['id']}").status_code == 403
    assert set_roles(b, sid, crew["member"]["id"], [admin["id"]]).status_code == 403
    assert set_roles(b, sid, crew["member"]["id"], [mod["id"]]).status_code == 403
    assert order_roles(b, sid, [mod["id"], admin["id"]]).status_code == 403


def test_a_member_can_create_and_manage_a_role_below_their_rank(crew):
    sid, b = crew["sid"], crew["b_client"]
    role = create_role(b, sid, "Mine", allow=bits(KICK))

    assert role["position"] == 1
    assert ranking(b, sid) == ["Admin", "Mod", "Mine", "@everyone"]
    assert patch_role(b, sid, role["id"], name="Still mine", color="#112233").status_code == 200
    assert set_roles(b, sid, crew["member"]["id"], [role["id"]]).status_code == 200
    assert set_roles(b, sid, crew["member"]["id"], []).status_code == 200
    assert b.delete(f"/servers/{sid}/roles/{role['id']}").status_code == 204


def test_a_member_cannot_switch_on_a_bit_they_do_not_hold(crew):
    sid, b = crew["sid"], crew["b_client"]
    role = create_role(b, sid, "Mine")

    for fields in ({"allow": bits(MANAGE_SERVER)}, {"deny": bits(MANAGE_SERVER)},
                   {"allow": bits(KICK, MANAGE_SERVER)}):
        res = patch_role(b, sid, role["id"], **fields)
        assert (res.status_code, res.json()["detail"]) == (
            403, "You can't change permissions you don't have")
    res = b.post(f"/servers/{sid}/roles", json={"name": "Greedy", "allow": bits(MANAGE_SERVER)})
    assert res.status_code == 403
    assert "Greedy" not in ranking(b, sid)
    assert patch_role(b, sid, role["id"], allow=bits(KICK), deny=bits(SEND)).status_code == 200


def test_a_member_cannot_inherit_from_admin(crew):
    sid, b = crew["sid"], crew["b_client"]
    admin = roles_by_name(b, sid)["Admin"]
    role = create_role(b, sid, "Mine")

    res = patch_role(b, sid, role["id"], parent_id=admin["id"])
    assert (res.status_code, res.json()["detail"]) == (
        403, "You can't change permissions you don't have")
    assert b.post(
        f"/servers/{sid}/roles", json={"name": "Heir", "parent_id": admin["id"]}).status_code == 403
    assert roles_by_name(b, sid)["Mine"]["parent_id"] is None


def test_a_member_cannot_move_a_role_to_or_above_their_rank(crew):
    sid, b = crew["sid"], crew["b_client"]
    mine = create_role(b, sid, "Mine")
    admin, mod = roles_by_name(b, sid)["Admin"], crew["mod"]

    res = order_roles(b, sid, [admin["id"], mine["id"], mod["id"]])
    assert (res.status_code, res.json()["detail"]) == (
        403, "That role is at or above your highest role")
    assert ranking(b, sid) == ["Admin", "Mod", "Mine", "@everyone"]


def test_a_member_can_reorder_roles_below_their_rank(crew):
    sid, b = crew["sid"], crew["b_client"]
    low = create_role(b, sid, "Low")
    lower = create_role(b, sid, "Lower")
    admin, mod = roles_by_name(b, sid)["Admin"], crew["mod"]

    res = order_roles(b, sid, [admin["id"], mod["id"], lower["id"], low["id"]])
    assert res.status_code == 200, res.text
    assert ranking(b, sid) == ["Admin", "Mod", "Lower", "Low", "@everyone"]


def test_editing_a_role_needs_to_outrank_everything_that_inherits_from_it(
        client, crew):
    sid, b = crew["sid"], crew["b_client"]
    low = create_role(client, sid, "Low")
    high = create_role(client, sid, "High", parent_id=low["id"])
    admin, mod = roles_by_name(client, sid)["Admin"], crew["mod"]
    assert order_roles(
        client, sid, [admin["id"], high["id"], mod["id"], low["id"]]).status_code == 200

    res = patch_role(b, sid, low["id"], name="Renamed")
    assert (res.status_code, res.json()["detail"]) == (
        403, "A role above yours inherits from this role")
    assert b.delete(f"/servers/{sid}/roles/{low['id']}").status_code == 403
    assert patch_role(b, sid, high["id"], name="Renamed").status_code == 403


def test_the_owner_skips_every_rule(client):
    register(client)
    sid = create_server(client)["id"]
    admin = roles_by_name(client, sid)["Admin"]

    assert patch_role(client, sid, admin["id"], allow=str(int(ALL_PERMISSIONS))).status_code == 200
    created = create_role(client, sid, "Top", parent_id=admin["id"])
    assert order_roles(client, sid, [created["id"], admin["id"]]).status_code == 200


def test_a_member_without_a_role_cannot_create_one(client, new_client):
    register(client)
    sid = create_server(client)["id"]
    joiner, user = join(client, new_client, sid)
    everyone = roles_by_name(client, sid)["@everyone"]
    assert patch_role(
        client, sid, everyone["id"], allow=bits(MEMBER_PERMISSIONS | MANAGE_ROLES)
    ).status_code == 200

    res = joiner.post(f"/servers/{sid}/roles", json={"name": "Mine"})
    assert (res.status_code, res.json()["detail"]) == (
        403, "That role is at or above your highest role")
    assert patch_role(joiner, sid, everyone["id"], deny="0").status_code == 403


# -- assigning ------------------------------------------------------------

def test_assigning_a_role_that_inherits_admin_needs_admins_bits(crew, client):
    sid, b = crew["sid"], crew["b_client"]
    admin = roles_by_name(client, sid)["Admin"]
    heir = create_role(client, sid, "Heir", parent_id=admin["id"])
    assert order_roles(client, sid, [admin["id"], crew["mod"]["id"], heir["id"]]).status_code == 200
    assert heir["allow"] == "0"

    res = set_roles(b, sid, crew["member"]["id"], [heir["id"]])
    assert (res.status_code, res.json()["detail"]) == (
        403, "You can't change permissions you don't have")
    assert set_roles(client, sid, crew["member"]["id"], [heir["id"]]).status_code == 200


def test_assigning_a_role_at_or_above_your_rank_is_refused(crew, client):
    sid, b = crew["sid"], crew["b_client"]
    below = create_role(b, sid, "Below")
    assert set_roles(b, sid, crew["member"]["id"], [below["id"]]).status_code == 200

    res = set_roles(b, sid, crew["member"]["id"], [below["id"], crew["mod"]["id"]])
    assert (res.status_code, res.json()["detail"]) == (
        403, "That role is at or above your highest role")
    assert set_roles(b, sid, crew["member"]["id"], []).status_code == 200
    assert set_roles(client, sid, crew["member"]["id"], [crew["mod"]["id"]]).status_code == 200
    assert set_roles(b, sid, crew["member"]["id"], []).status_code == 403


# -- realtime -------------------------------------------------------------

def test_only_members_whose_mask_changed_get_permissions_updated(crew, client):
    sid = crew["sid"]
    kicker = create_role(client, sid, "Kicker", allow=bits(KICK))
    set_roles(client, sid, crew["member"]["id"], [kicker["id"]])

    with crew["member_client"].websocket_connect("/ws", headers=HEADERS) as member_ws, \
            crew["other_client"].websocket_connect("/ws", headers=HEADERS) as other_ws:
        ws_ready(member_ws)
        ws_ready(other_ws)
        member_ws.receive_json()  # other came online

        res = patch_role(client, sid, kicker["id"], allow=bits(KICK, MANAGE_MESSAGES))
        assert res.status_code == 200

        expected_mask = str(int(MEMBER_PERMISSIONS | KICK | MANAGE_MESSAGES))
        assert member_ws.receive_json() == {
            "type": "permissions_updated", "server_id": sid, "permissions": expected_mask}
        role_updated = {"type": "role_updated", "server_id": sid, "role": res.json()}
        assert member_ws.receive_json() == role_updated
        assert other_ws.receive_json() == role_updated


def test_role_events_are_broadcast(crew, client):
    sid = crew["sid"]

    with crew["other_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)

        created = create_role(client, sid, "Fresh")
        frame = ws.receive_json()
        assert frame["type"] == "role_created"
        assert frame["server_id"] == sid
        assert frame["role"] == created
        assert frame["positions"] == [
            {"id": r["id"], "position": r["position"]}
            for r in client.get(f"/servers/{sid}/roles").json()]

        admin = roles_by_name(client, sid)["Admin"]
        order = [created["id"], crew["mod"]["id"], admin["id"]]
        assert order_roles(client, sid, order).status_code == 200
        frame = ws.receive_json()
        assert frame["type"] == "roles_reordered"
        assert {p["id"]: p["position"] for p in frame["positions"]}[created["id"]] == 3

        assert client.delete(f"/servers/{sid}/roles/{created['id']}").status_code == 204
        frame = ws.receive_json()
        assert frame["type"] == "role_deleted"
        assert frame["role_id"] == created["id"]
        assert created["id"] not in [p["id"] for p in frame["positions"]]


def test_deleting_a_role_tells_the_members_who_lost_bits(crew, client):
    sid = crew["sid"]
    kicker = create_role(client, sid, "Kicker", allow=bits(KICK))
    set_roles(client, sid, crew["member"]["id"], [kicker["id"]])

    with crew["member_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert client.delete(f"/servers/{sid}/roles/{kicker['id']}").status_code == 204

        assert ws.receive_json() == {
            "type": "permissions_updated", "server_id": sid,
            "permissions": str(int(MEMBER_PERMISSIONS))}
        assert ws.receive_json()["type"] == "role_deleted"


# -- voice ----------------------------------------------------------------

def test_denying_speak_takes_the_microphone_away_mid_call(crew, client, monkeypatch):
    sid, member_id = crew["sid"], crew["member"]["id"]
    lounge = crew["lounge"]["id"]
    quiet = create_role(client, sid, "Quiet")
    set_roles(client, sid, member_id, [quiet["id"]])
    source = use_presence(client, monkeypatch, {lounge: _in_voice(member_id)})

    assert patch_role(client, sid, quiet["id"], deny=bits(Permission.SPEAK)).status_code == 200

    assert source.removed == []
    assert source.updated == [
        (lounge, member_id, ["screen_share", "screen_share_audio"], {"server_muted": ""})]


def test_losing_connect_removes_the_member_from_the_call(crew, client, monkeypatch):
    sid, member_id = crew["sid"], crew["member"]["id"]
    lounge = crew["lounge"]["id"]
    kicked = create_role(client, sid, "Offline")
    set_roles(client, sid, member_id, [kicked["id"]])
    source = use_presence(client, monkeypatch, {lounge: _in_voice(member_id)})

    assert patch_role(client, sid, kicked["id"], deny=bits(Permission.CONNECT)).status_code == 200

    assert source.removed == [(lounge, member_id)]
    assert source.updated == []


def test_changes_that_leave_voice_bits_alone_do_not_touch_the_room(crew, client, monkeypatch):
    sid, member_id = crew["sid"], crew["member"]["id"]
    lounge = crew["lounge"]["id"]
    kicker = create_role(client, sid, "Kicker")
    set_roles(client, sid, member_id, [kicker["id"]])
    source = use_presence(client, monkeypatch, {lounge: _in_voice(member_id)})

    assert patch_role(client, sid, kicker["id"], allow=bits(KICK)).status_code == 200

    assert source.removed == [] and source.updated == []


def test_assigning_and_unassigning_a_role_updates_the_room(crew, client, monkeypatch):
    sid, member_id = crew["sid"], crew["member"]["id"]
    lounge = crew["lounge"]["id"]
    quiet = create_role(client, sid, "Quiet", deny=bits(Permission.SPEAK))
    source = use_presence(client, monkeypatch, {lounge: _in_voice(member_id)})

    assert set_roles(client, sid, member_id, [quiet["id"]]).status_code == 200

    assert source.updated == [
        (lounge, member_id, ["screen_share", "screen_share_audio"], {"server_muted": ""})]


def test_everyone_edit_reaches_members_in_voice(crew, client, monkeypatch):
    sid, member_id = crew["sid"], crew["member"]["id"]
    lounge = crew["lounge"]["id"]
    everyone = roles_by_name(client, sid)["@everyone"]
    source = use_presence(client, monkeypatch, {lounge: _in_voice(member_id)})

    assert patch_role(
        client, sid, everyone["id"], allow=bits(MEMBER_PERMISSIONS & ~Permission.CONNECT)
    ).status_code == 200

    assert (lounge, member_id) in source.removed
