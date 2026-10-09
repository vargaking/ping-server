"""Roles and permissions: the resolver, the REST matrix and the realtime frames."""
import pytest

from app.models.Role import Role
from app.models.RoleToUser import RoleToUser
from app.permissions import (
    ADMIN_PERMISSIONS,
    ALL_PERMISSIONS,
    MEMBER_PERMISSIONS,
    Permission,
)
from app.routers import voice
from app.services.permissions import permissions
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_realtime_events import chat_frame, invite_and_join

HEADERS = {"origin": ORIGIN}


def run(client, func, *args):
    """Run a coroutine function on the app's event loop, where the DB lives."""
    return client.portal.call(func, *args)


def roles_by_name(client, server_id):
    res = client.get(f"/servers/{server_id}/roles")
    assert res.status_code == 200, res.text
    return {role["name"]: role for role in res.json()}


def set_roles(owner_client, server_id, user_id, role_ids):
    return owner_client.put(
        f"/servers/{server_id}/members/{user_id}/roles", json={"role_ids": role_ids})


def promote(owner_client, server_id, user_id):
    admin_role = roles_by_name(owner_client, server_id)["Admin"]
    res = set_roles(owner_client, server_id, user_id, [admin_role["id"]])
    assert res.status_code == 200, res.text


def join(owner_client, new_client, server_id):
    joiner = new_client()
    user = register(joiner)
    invite_and_join(owner_client, joiner, server_id)
    return joiner, user


@pytest.fixture
def team(client, new_client):
    """An owner (the `client`), an admin, a second admin and a plain member."""
    owner = register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"])
    admin_client, admin = join(client, new_client, server["id"])
    admin2_client, admin2 = join(client, new_client, server["id"])
    member_client, member = join(client, new_client, server["id"])
    promote(client, server["id"], admin["id"])
    promote(client, server["id"], admin2["id"])
    return {
        "owner": owner, "server": server, "channel": channel,
        "admin_client": admin_client, "admin": admin,
        "admin2_client": admin2_client, "admin2": admin2,
        "member_client": member_client, "member": member,
    }


# -- new servers and the roles endpoint ----------------------------------

def test_new_server_has_exactly_default_and_admin_roles(client):
    register(client)
    server = create_server(client)

    roles = client.get(f"/servers/{server['id']}/roles").json()
    assert [
        (r["name"], r["is_default"], r["allow"], r["deny"], r["parent_id"], r["position"], r["color"])
        for r in roles
    ] == [
        ("Admin", False, str(int(ADMIN_PERMISSIONS)), "0", None, 1, None),
        ("@everyone", True, str(int(MEMBER_PERMISSIONS)), "0", None, 0, None),
    ]
    assert (int(MEMBER_PERMISSIONS), int(ADMIN_PERMISSIONS), int(ALL_PERMISSIONS)) == (123, 13311, 16383)


def test_roles_are_hidden_from_non_members(client, new_client):
    register(client)
    server = create_server(client)
    outsider = new_client()
    register(outsider)

    assert outsider.get(f"/servers/{server['id']}/roles").status_code == 404
    assert client.get("/servers/999999/roles").status_code == 404


def test_server_responses_carry_the_callers_permissions(team):
    sid = team["server"]["id"]
    assert team["member_client"].get(f"/servers/{sid}").json()["permissions"] == "123"
    assert team["admin_client"].get(f"/servers/{sid}").json()["permissions"] == "13311"

    mine = team["member_client"].get("/servers/me").json()
    assert [(s["id"], s["permissions"]) for s in mine] == [(sid, "123")]


def test_creating_a_server_returns_owner_permissions(client):
    register(client)
    assert create_server(client)["permissions"] == "16383"


def test_members_list_shows_assigned_roles_only(team, client):
    sid = team["server"]["id"]
    admin_role = roles_by_name(client, sid)["Admin"]

    members = {m["user"]["id"]: m for m in client.get(f"/servers/{sid}/members").json()}
    assert members[team["admin"]["id"]]["role_ids"] == [admin_role["id"]]
    assert members[team["member"]["id"]]["role_ids"] == []
    assert members[team["owner"]["id"]]["role_ids"] == []


# -- resolver ------------------------------------------------------------

def test_resolver_owner_member_admin_and_outsider(team, client, new_client):
    server_id = team["server"]["id"]

    async def masks():
        return [
            await permissions.effective(team[who]["id"], server_id)
            for who in ("owner", "member", "admin")
        ]

    owner, member, admin = run(client, masks)
    assert owner == ALL_PERMISSIONS
    assert member == MEMBER_PERMISSIONS
    assert admin == ADMIN_PERMISSIONS

    outsider = register(new_client())
    assert run(client, permissions.effective, outsider["id"], server_id) is None
    assert run(client, permissions.effective, team["member"]["id"], 999999) is None


def test_resolver_deny_clears_a_bit(team, client):
    server_id = team["server"]["id"]
    member_id = team["member"]["id"]

    async def setup():
        role = await Role.create(
            server_id=server_id, name="Muted", deny=int(Permission.SEND_MESSAGES))
        await RoleToUser.create(role_id=role.id, user_id=member_id)
        permissions.invalidate(server_id, member_id)
        return await permissions.effective(member_id, server_id)

    assert run(client, setup) == MEMBER_PERMISSIONS & ~Permission.SEND_MESSAGES


def test_resolver_ors_in_the_parent_chain(team, client):
    server_id = team["server"]["id"]
    member_id = team["member"]["id"]

    async def setup():
        grandparent = await Role.create(
            server_id=server_id, name="Grand", allow=int(Permission.MANAGE_SERVER))
        parent = await Role.create(
            server_id=server_id, name="Parent", allow=int(Permission.MANAGE_ROLES),
            parent_id=grandparent.id)
        child = await Role.create(server_id=server_id, name="Child", parent_id=parent.id)
        await RoleToUser.create(role_id=child.id, user_id=member_id)
        permissions.invalidate(server_id, member_id)
        return await permissions.effective(member_id, server_id)

    assert run(client, setup) == (
        MEMBER_PERMISSIONS | Permission.MANAGE_SERVER | Permission.MANAGE_ROLES)


def test_resolver_terminates_on_a_parent_cycle(team, client):
    server_id = team["server"]["id"]
    member_id = team["member"]["id"]

    async def setup():
        a = await Role.create(server_id=server_id, name="A", allow=int(Permission.MANAGE_SERVER))
        b = await Role.create(server_id=server_id, name="B", parent_id=a.id)
        a.parent_id = b.id
        await a.save()
        await RoleToUser.create(role_id=a.id, user_id=member_id)
        permissions.invalidate(server_id, member_id)
        return await permissions.effective(member_id, server_id)

    assert run(client, setup) == MEMBER_PERMISSIONS | Permission.MANAGE_SERVER


def test_resolver_cache_follows_assignment_removal_and_joining(client, new_client):
    register(client)
    server = create_server(client)
    joiner = new_client()
    user = register(joiner)
    sid = server["id"]

    assert run(client, permissions.effective, user["id"], sid) is None
    invite_and_join(client, joiner, sid)
    assert run(client, permissions.effective, user["id"], sid) == MEMBER_PERMISSIONS

    promote(client, sid, user["id"])
    assert run(client, permissions.effective, user["id"], sid) == ADMIN_PERMISSIONS

    assert set_roles(client, sid, user["id"], []).status_code == 200
    assert run(client, permissions.effective, user["id"], sid) == MEMBER_PERMISSIONS

    assert client.delete(f"/servers/{sid}/members/{user['id']}").status_code == 204
    assert run(client, permissions.effective, user["id"], sid) is None


# -- the REST matrix -----------------------------------------------------

def test_admin_manages_channels(team):
    admin, sid = team["admin_client"], team["server"]["id"]

    created = admin.post(f"/channels/{sid}/create", json={"name": "ideas"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert admin.patch(f"/channels/{cid}", json={"name": "plans"}).status_code == 200
    assert admin.delete(f"/channels/{cid}").status_code == 204


def test_admin_manages_invites(team):
    admin, sid = team["admin_client"], team["server"]["id"]

    invite = admin.post("/invites/", json={"server_id": sid})
    assert invite.status_code == 201, invite.text
    invite_id = invite.json()["id"]
    assert admin.put(f"/invites/{invite_id}", json={"is_active": False}).status_code == 200
    assert invite_id in [i["id"] for i in admin.get(f"/invites/server/{sid}").json()]
    assert admin.delete(f"/invites/{invite_id}").status_code == 204


def test_creator_edits_and_revokes_own_invite_without_manage_invites(team):
    member, sid = team["member_client"], team["server"]["id"]
    own = member.post("/invites/", json={"server_id": sid}).json()
    assert own["created_by_username"] == team["member"]["username"]

    updated = member.put(f"/invites/{own['id']}", json={"max_uses": 3, "valid_until": None})
    assert updated.status_code == 200, updated.text
    assert updated.json()["max_uses"] == 3
    assert updated.json()["created_by_username"] == team["member"]["username"]
    cleared = member.put(f"/invites/{own['id']}", json={"max_uses": None})
    assert cleared.json()["max_uses"] is None
    assert member.delete(f"/invites/{own['id']}").status_code == 204
    assert member.delete(f"/invites/{own['id']}").status_code == 404


def test_creator_cannot_touch_another_members_invite(team):
    member, admin, sid = team["member_client"], team["admin_client"], team["server"]["id"]
    admins_invite = admin.post("/invites/", json={"server_id": sid}).json()

    assert member.put(f"/invites/{admins_invite['id']}", json={"max_uses": 1}).status_code == 403
    assert member.delete(f"/invites/{admins_invite['id']}").status_code == 403


def test_creator_who_lost_create_invite_cannot_edit_their_invite(team, client):
    member, sid = team["member_client"], team["server"]["id"]
    own = member.post("/invites/", json={"server_id": sid}).json()

    _deny(client, team, "No invites", Permission.CREATE_INVITE)

    assert member.put(f"/invites/{own['id']}", json={"max_uses": 1}).status_code == 403
    assert member.delete(f"/invites/{own['id']}").status_code == 403


def test_invite_list_includes_creator_username(team, client):
    sid = team["server"]["id"]
    team["member_client"].post("/invites/", json={"server_id": sid})

    listed = client.get(f"/invites/server/{sid}").json()
    names = {i["created_by_id"]: i["created_by_username"] for i in listed}
    assert names[team["member"]["id"]] == team["member"]["username"]
    assert names[team["owner"]["id"]] == team["owner"]["username"]


def test_invite_list_mine_returns_only_the_callers_invites(team, client):
    sid = team["server"]["id"]
    own = client.post("/invites/", json={"server_id": sid}).json()
    team["admin_client"].post("/invites/", json={"server_id": sid})
    team["member_client"].post("/invites/", json={"server_id": sid})

    everything = client.get(f"/invites/server/{sid}").json()
    mine = client.get(f"/invites/server/{sid}", params={"mine": True})

    assert mine.status_code == 200
    assert {i["created_by_id"] for i in everything} > {team["owner"]["id"]}
    assert [i["id"] for i in mine.json()] == [
        i["id"] for i in everything if i["created_by_id"] == team["owner"]["id"]]
    assert own["id"] in [i["id"] for i in mine.json()]


def test_invite_list_mine_for_a_create_invite_only_member(team):
    member, sid = team["member_client"], team["server"]["id"]
    team["admin_client"].post("/invites/", json={"server_id": sid})
    own = member.post("/invites/", json={"server_id": sid}).json()

    for params in ({}, {"mine": True}):
        listed = member.get(f"/invites/server/{sid}", params=params)
        assert listed.status_code == 200
        assert [i["id"] for i in listed.json()] == [own["id"]]


def test_invite_list_needs_an_invite_permission_with_or_without_mine(team, client):
    member, sid = team["member_client"], team["server"]["id"]
    _deny(client, team, "No invites", Permission.CREATE_INVITE)

    assert member.get(f"/invites/server/{sid}").status_code == 403
    assert member.get(f"/invites/server/{sid}", params={"mine": True}).status_code == 403


def test_wrong_invite_password_is_forbidden_not_unauthorized(team, new_client):
    sid = team["server"]["id"]
    invite = team["admin_client"].post(
        "/invites/", json={"server_id": sid, "password": "hunter2"}).json()
    joiner = new_client()
    register(joiner)

    missing = joiner.post(f"/invites/{invite['id']}/use", json={})
    assert (missing.status_code, missing.json()["detail"]) == (403, "Password required")
    wrong = joiner.post(f"/invites/{invite['id']}/use", json={"password": "nope"})
    assert (wrong.status_code, wrong.json()["detail"]) == (403, "Incorrect password")
    assert joiner.post(
        f"/invites/{invite['id']}/use", json={"password": "hunter2"}).status_code == 200


def test_admin_kicks_a_member(team, client):
    sid = team["server"]["id"]
    res = team["admin_client"].delete(f"/servers/{sid}/members/{team['member']['id']}")
    assert res.status_code == 204
    ids = [m["user"]["id"] for m in client.get(f"/servers/{sid}/members").json()]
    assert team["member"]["id"] not in ids


def test_admin_cannot_manage_the_server_or_roles(team):
    admin, sid = team["admin_client"], team["server"]["id"]

    assert admin.put(f"/servers/{sid}", json={"name": "Mine now"}).status_code == 403
    assert admin.put(f"/servers/{sid}", json={"server_profile": {"x": 1}}).status_code == 403
    assert admin.delete(f"/servers/{sid}").status_code == 403
    assert admin.post(
        f"/servers/{sid}/icon", files={"file": ("i.png", b"x", "image/png")}).status_code == 403

    admin_role = roles_by_name(admin, sid)["Admin"]
    res = set_roles(admin, sid, team["member"]["id"], [admin_role["id"]])
    assert res.status_code == 403


def test_admin_cannot_kick_another_admin_or_the_owner(team, client):
    admin, sid = team["admin_client"], team["server"]["id"]

    res = admin.delete(f"/servers/{sid}/members/{team['admin2']['id']}")
    assert res.status_code == 403
    assert "kick" in res.json()["detail"]
    assert admin.delete(f"/servers/{sid}/members/{team['owner']['id']}").status_code == 403
    assert len(client.get(f"/servers/{sid}/members").json()) == 4


def test_owner_can_kick_an_admin(team, client):
    sid = team["server"]["id"]
    assert client.delete(f"/servers/{sid}/members/{team['admin']['id']}").status_code == 204


def test_member_is_limited_to_reading_chatting_and_own_invites(team, client):
    member, sid = team["member_client"], team["server"]["id"]
    cid = team["channel"]["id"]

    assert member.post(f"/channels/{sid}/create", json={"name": "nope"}).status_code == 403
    assert member.patch(f"/channels/{cid}", json={"name": "nope"}).status_code == 403
    assert member.delete(f"/channels/{cid}").status_code == 403
    assert member.delete(f"/servers/{sid}/members/{team['admin']['id']}").status_code == 403
    assert member.delete(f"/servers/{sid}/members/{team['admin2']['id']}").status_code == 403

    assert member.get(f"/channels/{sid}").status_code == 200
    assert member.get(f"/channels/{cid}/messages").status_code == 200

    owners_invite = client.post("/invites/", json={"server_id": sid}).json()
    own = member.post("/invites/", json={"server_id": sid})
    assert own.status_code == 201, own.text

    listed = member.get(f"/invites/server/{sid}")
    assert listed.status_code == 200
    assert [i["id"] for i in listed.json()] == [own.json()["id"]]
    assert owners_invite["id"] in [i["id"] for i in client.get(f"/invites/server/{sid}").json()]

    assert member.put(f"/invites/{owners_invite['id']}", json={"is_active": False}).status_code == 403
    assert member.delete(f"/invites/{owners_invite['id']}").status_code == 403
    assert member.put(f"/invites/{own.json()['id']}", json={"max_uses": 5}).json()["max_uses"] == 5
    assert member.delete(f"/invites/{own.json()['id']}").status_code == 204


def test_non_members_get_403_or_404_never_data(team, new_client):
    outsider = new_client()
    register(outsider)
    sid, cid = team["server"]["id"], team["channel"]["id"]

    assert outsider.post(f"/channels/{sid}/create", json={"name": "x"}).status_code == 403
    assert outsider.get(f"/channels/{cid}/messages").status_code == 403
    assert outsider.post("/invites/", json={"server_id": sid}).status_code == 403
    assert outsider.get(f"/invites/server/{sid}").status_code == 403
    assert outsider.get(f"/servers/{sid}").status_code == 404


def test_demoted_admin_loses_access_on_the_next_request(team, client):
    admin, sid = team["admin_client"], team["server"]["id"]
    assert admin.post(f"/channels/{sid}/create", json={"name": "before"}).status_code == 201

    assert set_roles(client, sid, team["admin"]["id"], []).status_code == 200
    assert admin.post(f"/channels/{sid}/create", json={"name": "after"}).status_code == 403


def test_channel_order_update_needs_only_manage_channels(team, client):
    admin, sid = team["admin_client"], team["server"]["id"]
    second = create_channel(client, sid, "second")
    order = [second["id"], team["channel"]["id"]]

    res = admin.put(f"/servers/{sid}", json={"server_settings": {"channel_order": order}})
    assert res.status_code == 200, res.text
    assert client.get(f"/servers/{sid}").json()["server_settings"]["channel_order"] == [
        team["channel"]["id"], second["id"]]

    res = admin.put(f"/servers/{sid}", json={
        "name": "Renamed", "server_settings": {"channel_order": order}})
    assert res.status_code == 403
    res = admin.put(f"/servers/{sid}", json={
        "server_settings": {"channel_order": order, "other": True}})
    assert res.status_code == 403
    assert client.get(f"/servers/{sid}").json()["name"] != "Renamed"

    member_res = team["member_client"].put(
        f"/servers/{sid}", json={"server_settings": {"channel_order": order}})
    assert member_res.status_code == 403


def test_channel_order_update_keeps_default_channel(team, client):
    admin, sid = team["admin_client"], team["server"]["id"]
    channel_id = team["channel"]["id"]
    second = create_channel(client, sid, "second")
    res = client.put(f"/servers/{sid}", json={"server_settings": {"default_channel_id": channel_id}})
    assert res.status_code == 200, res.text

    order = [second["id"], channel_id]
    res = admin.put(f"/servers/{sid}", json={"server_settings": {"channel_order": order}})
    assert res.status_code == 200, res.text
    settings = client.get(f"/servers/{sid}").json()["server_settings"]
    assert settings["channel_order"] == [channel_id, second["id"]]
    assert settings["default_channel_id"] == channel_id

    res = admin.put(f"/servers/{sid}", json={"server_settings": {"default_channel_id": None}})
    assert res.status_code == 403


def test_moderators_delete_other_peoples_messages_but_members_do_not(team, client):
    sid, cid = team["server"]["id"], team["channel"]["id"]
    member_client, admin_client = team["member_client"], team["admin_client"]

    def post_as_admin2():
        frame = chat_frame(sid, cid)
        # A second socket of the same user hears the message once it is stored.
        with team["admin2_client"].websocket_connect("/ws", headers=HEADERS) as ws, \
                team["admin2_client"].websocket_connect("/ws", headers=HEADERS) as echo:
            ws_ready(ws)
            ws_ready(echo)
            ws.send_json(frame)
            assert echo.receive_json()["id"] == frame["id"]
        return frame["id"]

    first, second = post_as_admin2(), post_as_admin2()
    assert member_client.delete(f"/messages/{first}").status_code == 403
    assert admin_client.delete(f"/messages/{first}").status_code == 204
    assert client.delete(f"/messages/{second}").status_code == 204


# -- deny is honoured everywhere a permission is checked -----------------

def _deny(client, team, name, bit):
    """Give the plain member a role that denies *bit*."""
    sid, member_id = team["server"]["id"], team["member"]["id"]

    async def make():
        role = await Role.create(server_id=sid, name=name, deny=int(bit))
        return role.id

    role_id = run(client, make)
    assert set_roles(client, sid, member_id, [role_id]).status_code == 200


def test_denied_view_channel_blocks_reads_and_downloads(team, client):
    _deny(client, team, "Blind", Permission.VIEW_CHANNEL)
    member, sid, cid = team["member_client"], team["server"]["id"], team["channel"]["id"]

    assert member.get(f"/channels/{cid}/messages").status_code == 403
    assert member.get(f"/channels/{sid}").status_code == 403
    upload = client.post(
        "/attachments", files={"file": ("a.txt", b"hello", "text/plain")},
        data={"channel_id": str(cid)})
    assert upload.status_code == 201, upload.text
    assert member.get(f"/attachments/{upload.json()['id']}").status_code == 404
    assert member.post(
        "/attachments", files={"file": ("a.txt", b"hello", "text/plain")},
        data={"channel_id": str(cid)}).status_code == 403


def test_denied_send_messages_is_a_forbidden_frame(team, client):
    _deny(client, team, "Muted", Permission.SEND_MESSAGES)
    sid, cid = team["server"]["id"], team["channel"]["id"]

    frame = chat_frame(sid, cid)
    with team["member_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(frame)
        assert ws.receive_json() == {"type": "error", "code": "forbidden", "ref": frame["id"]}


def test_denied_connect_blocks_the_voice_token(team, client, monkeypatch):
    monkeypatch.setattr(voice, "LIVEKIT_URL", "wss://livekit.test")
    monkeypatch.setattr(voice, "LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr(voice, "LIVEKIT_API_SECRET", "test-secret-" + "x" * 32)
    room = create_channel(client, team["server"]["id"], "Hangout", "voice")
    member = team["member_client"]

    assert member.post("/api/voice/token", json={"channel_id": room["id"]}).status_code == 200
    _deny(client, team, "Silenced", Permission.CONNECT)
    res = member.post("/api/voice/token", json={"channel_id": room["id"]})
    assert res.status_code == 403
    assert res.json()["detail"] == "Missing permission"


# -- PUT /servers/{id}/members/{user_id}/roles ---------------------------

def test_role_assignment_rules(team, client, new_client):
    sid, member_id = team["server"]["id"], team["member"]["id"]
    roles = roles_by_name(client, sid)
    admin_id, default_id = roles["Admin"]["id"], roles["@everyone"]["id"]

    res = set_roles(client, sid, member_id, [admin_id])
    assert res.status_code == 200, res.text
    assert res.json()["role_ids"] == [admin_id]
    assert res.json()["user"]["id"] == member_id

    assert set_roles(client, sid, member_id, [default_id]).status_code == 422
    assert set_roles(client, sid, member_id, [999999]).status_code == 422
    assert set_roles(client, sid, team["owner"]["id"], [admin_id]).status_code == 403

    outsider = register(new_client())
    assert set_roles(client, sid, outsider["id"], [admin_id]).status_code == 404

    other = create_server(client, "Other")
    foreign_admin = roles_by_name(client, other["id"])["Admin"]["id"]
    assert set_roles(client, sid, member_id, [foreign_admin]).status_code == 422

    assert set_roles(client, sid, member_id, []).json()["role_ids"] == []
    assert set_roles(team["member_client"], sid, member_id, [admin_id]).status_code == 403


# -- websocket frames ----------------------------------------------------

def test_permissions_init_lists_every_server_the_user_is_in(team, client):
    sid = team["server"]["id"]
    other = create_server(client, "Second")

    with team["member_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        presence = ws.receive_json()
        assert presence["type"] == "presence_init"
        assert ws.receive_json() == {
            "type": "permissions_init", "servers": {str(sid): "123"}, "channels": {str(sid): {}}}

    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready_frames = [ws.receive_json(), ws.receive_json()]
        assert ws_ready_frames[1] == {
            "type": "permissions_init",
            "servers": {str(sid): "16383", str(other["id"]): "16383"},
            "channels": {str(sid): {}, str(other["id"]): {}},
        }


def test_permissions_init_is_sent_even_when_empty(client):
    register(client)
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        assert ws.receive_json()["type"] == "presence_init"
        assert ws.receive_json() == {"type": "permissions_init", "servers": {}, "channels": {}}


def test_promote_and_demote_notify_the_target_and_the_rest(team, client):
    sid, member = team["server"]["id"], team["member"]
    admin_role = roles_by_name(client, sid)["Admin"]["id"]

    with team["member_client"].websocket_connect("/ws", headers=HEADERS) as member_ws, \
            team["admin_client"].websocket_connect("/ws", headers=HEADERS) as admin_ws, \
            client.websocket_connect("/ws", headers=HEADERS) as owner_ws:
        ws_ready(member_ws)
        ws_ready(admin_ws)
        ws_ready(owner_ws)
        # Drain the presence_update frames the later connections caused.
        member_ws.receive_json(); member_ws.receive_json()
        admin_ws.receive_json()

        assert set_roles(client, sid, member["id"], [admin_role]).status_code == 200
        roles_updated = {
            "type": "member_roles_updated", "server_id": sid,
            "user_id": member["id"], "role_ids": [admin_role],
        }
        assert admin_ws.receive_json() == roles_updated
        assert owner_ws.receive_json() == roles_updated
        got = {f["type"]: f for f in (member_ws.receive_json(), member_ws.receive_json())}
        assert got["member_roles_updated"] == roles_updated
        assert got["permissions_updated"] == {
            "type": "permissions_updated", "server_id": sid, "permissions": "13311",
            "channels": {}}

        assert set_roles(client, sid, member["id"], []).status_code == 200
        demoted = {**roles_updated, "role_ids": []}
        assert admin_ws.receive_json() == demoted
        assert owner_ws.receive_json() == demoted
        got = {f["type"]: f for f in (member_ws.receive_json(), member_ws.receive_json())}
        assert got["member_roles_updated"] == demoted
        assert got["permissions_updated"]["permissions"] == "123"


def test_no_permissions_updated_when_the_mask_does_not_change(team, client):
    sid, member_id = team["server"]["id"], team["member"]["id"]

    async def make():
        return (await Role.create(server_id=sid, name="Cosmetic")).id

    cosmetic = run(client, make)
    admin_role = roles_by_name(client, sid)["Admin"]["id"]

    with team["member_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert set_roles(client, sid, member_id, [cosmetic]).status_code == 200
        assert ws.receive_json()["type"] == "member_roles_updated"

        assert set_roles(client, sid, member_id, [cosmetic, admin_role]).status_code == 200
        assert ws.receive_json()["type"] == "member_roles_updated"
        assert ws.receive_json()["type"] == "permissions_updated"


def test_kicked_member_is_told_and_loses_the_server(team, client):
    sid, member = team["server"]["id"], team["member"]
    with team["member_client"].websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert client.delete(f"/servers/{sid}/members/{member['id']}").status_code == 204
        assert ws.receive_json()["type"] == "member_left"
    assert run(client, permissions.effective, member["id"], sid) is None
