"""Channel and category permission overwrites: the resolver, who can edit
them, and every route, socket handler and fan-out with a member who is in
the server but can't view the channel."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.PermissionOverwrite import PermissionOverwrite
from app.permissions import ALL_PERMISSIONS, MEMBER_PERMISSIONS, Permission
from app.routers import voice
from app.services import role_resolution
from app.services.permissions import member_views, permissions, server_masks
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_attachments import upload
from tests.test_permissions import join, roles_by_name, run, set_roles

HEADERS = {"origin": ORIGIN}
VIEW = int(Permission.VIEW_CHANNEL)
SEND = int(Permission.SEND_MESSAGES)
SPEAK = int(Permission.SPEAK)


def doc(text="hi"):
    return {"type": "doc", "content": [{"type": "paragraph",
                                        "content": [{"type": "text", "text": text}]}]}


def chat_frame(server_id, channel_id, text="hi"):
    return {
        "type": "message", "id": str(uuid.uuid4()), "server_id": server_id,
        "channel_id": channel_id, "content": doc(text),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def overwrite(client, path, allow=0, deny=0):
    return client.put(path, json={"allow": str(allow), "deny": str(deny)})


@pytest.fixture
def crew(client, new_client):
    """Owner A (the `client`), B with a Mod role that can manage roles, and a
    plain member C. `secret` is private to Mod; `general` is open."""
    owner = register(client)
    server = create_server(client)
    sid = server["id"]
    general = create_channel(client, sid, "general")
    secret = create_channel(client, sid, "secret")
    mod_role = client.post(f"/servers/{sid}/roles", json={
        "name": "Mod", "allow": str(int(MEMBER_PERMISSIONS | Permission.MANAGE_ROLES))})
    assert mod_role.status_code == 201, mod_role.text
    mod_role = mod_role.json()
    mod_client, mod = join(client, new_client, sid)
    member_client, member = join(client, new_client, sid)
    assert set_roles(client, sid, mod["id"], [mod_role["id"]]).status_code == 200
    everyone = roles_by_name(client, sid)["@everyone"]
    base = f"/channels/{secret['id']}/permissions"
    assert overwrite(client, f"{base}/roles/{mod_role['id']}", allow=VIEW).status_code == 200
    assert overwrite(client, f"{base}/roles/{everyone['id']}", deny=VIEW).status_code == 200
    return SimpleNamespace(
        owner_client=client, owner=owner, server=server, sid=sid, general=general,
        secret=secret, mod_role=mod_role, mod_client=mod_client, mod=mod,
        member_client=member_client, member=member, everyone=everyone)


def post_message(client, sid, channel_id, text="hi"):
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        frame = chat_frame(sid, channel_id, text)
        ws.send_json(frame)
        while True:
            reply = ws.receive_json()
            if reply.get("type") == "message_ack" and reply.get("id") == frame["id"]:
                return frame["id"]
            assert reply.get("type") != "error", reply


# -- the resolver ------------------------------------------------------------

def _role(id, position, allow=0, deny=0, parent_id=None, is_default=False):
    return SimpleNamespace(id=id, position=position, allow=allow, deny=deny,
                           parent_id=parent_id, is_default=is_default)


ROLES = {
    1: _role(1, 0, allow=int(MEMBER_PERMISSIONS), is_default=True),
    2: _role(2, 1, allow=int(Permission.MANAGE_MESSAGES)),
    3: _role(3, 2, parent_id=2),
}


def mask(assigned, layers, roles=ROLES):
    server = role_resolution.member_mask(roles, assigned)
    return int(role_resolution.channel_mask(server, roles, assigned, layers))


def test_member_mask_is_unchanged_by_the_layer_refactor():
    assert int(role_resolution.member_mask(ROLES, [])) == int(MEMBER_PERMISSIONS)
    assert int(role_resolution.member_mask(ROLES, [3])) == int(
        MEMBER_PERMISSIONS | Permission.MANAGE_MESSAGES)


def test_no_rows_change_nothing():
    assert mask([2], []) == mask([2], [({}, None)]) == int(
        MEMBER_PERMISSIONS | Permission.MANAGE_MESSAGES)


def test_everyone_deny_then_higher_role_allow():
    layers = [({1: (0, VIEW), 2: (VIEW, 0)}, None)]
    assert not mask([], layers) & VIEW
    assert mask([2], layers) & VIEW


def test_the_highest_role_that_says_anything_decides():
    layers = [({2: (SEND, 0), 3: (0, SEND)}, None)]
    assert not mask([2, 3], layers) & SEND


def test_a_role_without_a_row_inherits_its_parents_row():
    layers = [({1: (0, VIEW), 2: (VIEW, 0)}, None)]
    assert mask([3], layers) & VIEW


def test_member_row_beats_roles_and_channel_beats_category():
    category = ({1: (0, VIEW)}, None)
    channel = ({}, (VIEW, 0))
    assert not mask([], [category]) & role_resolution.CHANNEL_BITS
    assert mask([], [category, channel]) & VIEW
    assert not mask([], [({}, None), ({}, (0, VIEW))]) & VIEW


def test_without_view_every_channel_bit_is_off_but_server_bits_stay():
    roles = {**ROLES, 2: _role(2, 1, allow=int(Permission.MANAGE_ROLES))}
    result = mask([2], [({1: (0, VIEW)}, None)], roles)
    assert result & role_resolution.CHANNEL_BITS == 0
    assert result & Permission.MANAGE_ROLES


def test_only_channel_bits_move():
    result = mask([], [({1: (int(Permission.MANAGE_SERVER), 0)}, None)])
    assert not result & Permission.MANAGE_SERVER


# -- the migration leaves every mask as it was --------------------------------

def test_with_no_rows_every_channel_mask_is_the_server_mask(client, new_client):
    register(client)
    server = create_server(client)
    sid = server["id"]
    group = client.post(f"/servers/{sid}/channel-groups", json={"name": "Nested"}).json()
    for name in ("a", "b"):
        create_channel(client, sid, name)
    client.post(f"/channels/{sid}/create", json={"name": "c", "group_id": group["id"]})
    parent = client.post(f"/servers/{sid}/roles", json={
        "name": "Parent", "allow": str(int(Permission.MANAGE_MESSAGES))}).json()
    child = client.post(f"/servers/{sid}/roles", json={
        "name": "Child", "parent_id": parent["id"], "deny": str(SEND)}).json()
    members = [join(client, new_client, sid)[1] for _ in range(3)]
    set_roles(client, sid, members[0]["id"], [child["id"]])
    set_roles(client, sid, members[1]["id"], [parent["id"]])

    async def check():
        from app.models.Server import Server
        model = await Server.get(id=sid)
        servers = await server_masks(model)
        views = await member_views(model)
        return servers, views

    servers, views = run(client, check)
    assert set(servers) == set(views)
    for user_id, view in views.items():
        assert view.mask == servers[user_id]
        assert view.differing_masks() == {}
        for channel_mask in view.channels.values():
            assert channel_mask == servers[user_id]


# -- edit rules ----------------------------------------------------------------

def test_listing_shows_rows_to_role_managers_only(crew):
    rows = crew.owner_client.get(f"/channels/{crew.secret['id']}/permissions").json()
    assert rows == {
        "roles": [
            {"role_id": crew.mod_role["id"], "allow": str(VIEW), "deny": "0"},
            {"role_id": crew.everyone["id"], "allow": "0", "deny": str(VIEW)},
        ],
        "members": [],
    }
    assert crew.mod_client.get(f"/channels/{crew.secret['id']}/permissions").status_code == 200
    assert crew.member_client.get(
        f"/channels/{crew.general['id']}/permissions").status_code == 403


def test_bad_bodies_are_422(crew):
    path = f"/channels/{crew.general['id']}/permissions/roles/{crew.everyone['id']}"
    assert overwrite(crew.owner_client, path, allow=int(Permission.MANAGE_SERVER)).status_code == 422
    assert overwrite(crew.owner_client, path, allow=VIEW, deny=VIEW).status_code == 422
    assert crew.owner_client.put(path, json={"allow": "x"}).status_code == 422


def test_empty_masks_delete_the_row(crew):
    path = f"/channels/{crew.general['id']}/permissions/members/{crew.member['id']}"
    assert overwrite(crew.owner_client, path, deny=SEND).status_code == 200
    assert overwrite(crew.owner_client, path).status_code == 204
    assert crew.owner_client.delete(path).status_code == 204
    assert run(crew.owner_client, PermissionOverwrite.filter(
        channel_id=crew.general["id"]).count) == 0


def test_unknown_subjects_are_404(crew, new_client):
    outsider = new_client()
    stranger = register(outsider)
    base = f"/channels/{crew.general['id']}/permissions"
    assert overwrite(crew.owner_client, f"{base}/roles/99999", deny=SEND).status_code == 404
    assert overwrite(
        crew.owner_client, f"{base}/members/{stranger['id']}", deny=SEND).status_code == 404


def test_a_manager_cant_touch_roles_or_members_at_or_above_them(crew):
    admin = roles_by_name(crew.owner_client, crew.sid)["Admin"]
    base = f"/channels/{crew.general['id']}/permissions"
    assert overwrite(crew.mod_client, f"{base}/roles/{admin['id']}", deny=SEND).status_code == 403
    assert overwrite(
        crew.mod_client, f"{base}/roles/{crew.mod_role['id']}", deny=SEND).status_code == 403
    assert overwrite(
        crew.mod_client, f"{base}/members/{crew.owner['id']}", deny=SEND).status_code == 403
    assert overwrite(
        crew.mod_client, f"{base}/members/{crew.member['id']}", deny=SEND).status_code == 200


def test_a_manager_cant_grant_a_bit_they_dont_hold_there(crew):
    path = f"/channels/{crew.general['id']}/permissions/members/{crew.member['id']}"
    manage = int(Permission.MANAGE_MESSAGES)
    assert overwrite(crew.mod_client, path, allow=manage).status_code == 403
    assert overwrite(crew.owner_client, path, allow=manage).status_code == 200


def test_a_manager_cant_take_away_their_own_view(crew):
    path = f"/channels/{crew.secret['id']}/permissions/roles/{crew.everyone['id']}"
    # Clearing @everyone's deny is fine; Mod still sees it.
    assert overwrite(crew.mod_client, path).status_code == 204
    member_path = f"/channels/{crew.general['id']}/permissions/members/{crew.mod['id']}"
    # A member row for yourself is above your own rank.
    assert overwrite(crew.mod_client, member_path, deny=VIEW).status_code == 403
    everyone_general = f"/channels/{crew.general['id']}/permissions/roles/{crew.everyone['id']}"
    res = overwrite(crew.mod_client, everyone_general, deny=VIEW)
    assert res.status_code == 403
    assert res.json()["detail"] == "You would lose access to this channel"
    assert run(crew.owner_client, PermissionOverwrite.filter(
        channel_id=crew.general["id"]).count) == 0


def test_a_hidden_channel_is_404_for_its_overwrite_routes(crew):
    base = f"/channels/{crew.secret['id']}/permissions"
    assert crew.member_client.get(base).status_code == 404
    assert overwrite(
        crew.member_client, f"{base}/members/{crew.member['id']}", allow=VIEW).status_code == 404


# -- every route with a member who can't view the channel -----------------------

def test_lists_leave_the_channel_out(crew):
    c, sid, secret = crew.member_client, crew.sid, crew.secret["id"]
    assert secret not in [ch["id"] for ch in c.get(f"/channels/{sid}").json()]
    snapshot = c.get(f"/servers/{sid}/channels").json()
    assert secret not in [ch["id"] for ch in snapshot["channels"]]
    for response in (c.get(f"/servers/{sid}").json(),
                     next(s for s in c.get("/servers/me").json() if s["id"] == sid)):
        assert secret not in response["server_settings"]["channel_order"]
        assert response["channel_permissions"] == {}
    mine = crew.mod_client.get(f"/servers/{sid}").json()
    assert secret in mine["server_settings"]["channel_order"]
    assert [ch["private"] for ch in crew.mod_client.get(f"/channels/{sid}").json()
            if ch["id"] == secret] == [True]


def test_default_channel_is_left_out_for_members_who_cant_view_it(crew):
    res = crew.owner_client.put(f"/servers/{crew.sid}", json={
        "server_settings": {"default_channel_id": crew.secret["id"]}})
    assert res.status_code == 200
    settings = crew.member_client.get(f"/servers/{crew.sid}").json()["server_settings"]
    assert "default_channel_id" not in settings
    settings = crew.mod_client.get(f"/servers/{crew.sid}").json()["server_settings"]
    assert settings["default_channel_id"] == crew.secret["id"]


def test_channel_routes_are_404(crew, monkeypatch):
    c, secret = crew.member_client, crew.secret["id"]
    message_id = post_message(crew.mod_client, crew.sid, secret)
    assert c.get(f"/channels/{secret}/messages").status_code == 404
    assert c.put(f"/channels/{secret}/read", json={"message_id": message_id}).status_code == 404
    assert c.patch(f"/messages/{message_id}", json={"content": doc()}).status_code == 404
    assert c.delete(f"/messages/{message_id}").status_code == 404
    assert c.put(f"/messages/{message_id}/reactions/%F0%9F%91%8D").status_code == 404
    assert c.delete(f"/messages/{message_id}/reactions/%F0%9F%91%8D").status_code == 404
    assert c.get(f"/channels/{secret}/viewers").status_code == 404
    assert c.patch(f"/channels/{secret}", json={"name": "x"}).status_code == 404
    assert c.delete(f"/channels/{secret}").status_code == 404
    assert upload(c, channel_id=secret).status_code == 404

    attachment = upload(crew.mod_client, channel_id=secret).json()
    assert crew.mod_client.get(attachment["url"]).status_code == 200
    assert c.get(attachment["url"]).status_code == 404

    viewers = crew.mod_client.get(f"/channels/{secret}/viewers").json()["user_ids"]
    assert sorted(viewers) == sorted([crew.owner["id"], crew.mod["id"]])


def test_forum_routes_are_404(crew):
    owner, sid = crew.owner_client, crew.sid
    forum = create_channel(owner, sid, "ideas", "forum")
    path = f"/channels/{forum['id']}/permissions/roles/{crew.everyone['id']}"
    assert overwrite(owner, path, deny=VIEW).status_code == 200
    post = owner.post(f"/channels/{forum['id']}/posts", json={
        "title": "Plan", "message": {
            "id": str(uuid.uuid4()), "content": doc(),
            "timestamp": datetime.now(timezone.utc).isoformat()}})
    assert post.status_code == 201, post.text
    post_id = post.json()["post"]["id"]

    c = crew.member_client
    assert c.get(f"/channels/{forum['id']}/posts").status_code == 404
    assert c.get(f"/channels/{forum['id']}/posts/{post_id}").status_code == 404
    assert c.get(f"/channels/{forum['id']}/tags").status_code == 404
    assert c.post(f"/channels/{forum['id']}/tags", json={"name": "x"}).status_code == 404
    assert c.patch(f"/posts/{post_id}", json={"title": "Mine"}).status_code == 404
    assert c.delete(f"/posts/{post_id}").status_code == 404
    assert c.post(f"/channels/{forum['id']}/posts", json={
        "title": "Sneaky", "message": {
            "id": str(uuid.uuid4()), "content": doc(),
            "timestamp": datetime.now(timezone.utc).isoformat()}}).status_code == 404
    assert c.get(f"/channels/{forum['id']}/messages", params={"post_id": post_id}).status_code == 404


def test_voice_routes(crew, monkeypatch):
    owner, sid = crew.owner_client, crew.sid
    monkeypatch.setattr(voice, "LIVEKIT_URL", "wss://livekit.test")
    monkeypatch.setattr(voice, "LIVEKIT_API_KEY", "key")
    monkeypatch.setattr(voice, "LIVEKIT_API_SECRET", "secret" * 6)
    room = create_channel(owner, sid, "room", "voice")
    path = f"/channels/{room['id']}/permissions/roles/{crew.everyone['id']}"
    assert overwrite(owner, path, deny=VIEW).status_code == 200

    c = crew.member_client
    assert c.post("/api/voice/token", json={"channel_id": room["id"]}).status_code == 404
    assert c.post(f"/api/voice/presence/channels/{room['id']}/refresh").status_code == 404
    assert crew.mod_client.post(
        "/api/voice/token", json={"channel_id": room["id"]}).status_code == 404

    assert overwrite(owner, path, deny=int(Permission.CONNECT)).status_code == 200
    assert c.post("/api/voice/token", json={"channel_id": room["id"]}).status_code == 403
    assert c.post(f"/api/voice/presence/channels/{room['id']}/refresh").status_code == 204


def test_a_channel_rule_cant_hide_a_channel_a_server_rule_allows_reading_by_id(crew):
    """A member without View in the whole server keeps the old 403."""
    owner, sid = crew.owner_client, crew.sid
    everyone = crew.everyone
    res = owner.patch(f"/servers/{sid}/roles/{everyone['id']}", json={
        "allow": str(int(MEMBER_PERMISSIONS) & ~VIEW)})
    assert res.status_code == 200, res.text
    assert crew.member_client.get(
        f"/channels/{crew.general['id']}/messages").status_code == 403


# -- sockets ---------------------------------------------------------------------

def test_a_hidden_member_gets_none_of_the_channels_frames(crew):
    sid, secret, general = crew.sid, crew.secret["id"], crew.general["id"]
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as member_ws, \
            crew.mod_client.websocket_connect("/ws", headers=HEADERS) as mod_ws:
        ws_ready(member_ws)
        ws_ready(mod_ws)
        member_ws.receive_json()  # mod online
        frame = chat_frame(sid, secret)
        mod_ws.send_json(frame)
        mod_ws.send_json({"type": "typing", "server_id": sid, "channel_id": secret})
        while mod_ws.receive_json().get("type") != "message_ack":
            pass
        mid = frame["id"]
        assert crew.mod_client.put(f"/messages/{mid}/reactions/%F0%9F%91%8D").status_code == 204
        assert crew.mod_client.patch(
            f"/messages/{mid}", json={"content": doc("edited")}).status_code == 200
        assert crew.mod_client.delete(f"/messages/{mid}").status_code == 204
        assert crew.owner_client.patch(
            f"/channels/{secret}", json={"topic": "quiet"}).status_code == 200

        # The member's next frame is the open channel's message, so nothing
        # from the private one was queued before it.
        marker = chat_frame(sid, general, "open")
        mod_ws.send_json(marker)
        received = member_ws.receive_json()
        assert received["type"] == "message" and received["channel_id"] == general


def test_a_hidden_member_cant_send_or_type(crew):
    sid, secret = crew.sid, crew.secret["id"]
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        frame = chat_frame(sid, secret)
        ws.send_json(frame)
        reply = ws.receive_json()
        assert reply["type"] == "error" or reply.get("code") == "forbidden", reply
    assert crew.mod_client.get(
        f"/channels/{secret}/messages").json()["messages"] == []


def test_send_denied_on_one_channel(crew):
    sid, general = crew.sid, crew.general["id"]
    path = f"/channels/{general}/permissions/members/{crew.member['id']}"
    assert overwrite(crew.owner_client, path, deny=SEND).status_code == 200
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(chat_frame(sid, general))
        reply = ws.receive_json()
        assert reply["type"] == "error" or reply.get("code") == "forbidden", reply
    assert crew.member_client.get(f"/channels/{general}/messages").status_code == 200


# -- visibility changes over the socket --------------------------------------------

def _frames_until(ws, frame_type):
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames


def test_losing_and_gaining_view_over_the_socket(crew):
    sid, general = crew.sid, crew.general["id"]
    path = f"/channels/{general}/permissions/roles/{crew.everyone['id']}"
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert overwrite(crew.owner_client, path, deny=VIEW).status_code == 200
        frames = _frames_until(ws, "channel_deleted")
        assert frames[-1] == {"type": "channel_deleted", "server_id": sid, "channel_id": general}

        member_path = f"/channels/{general}/permissions/members/{crew.member['id']}"
        assert overwrite(crew.owner_client, member_path, allow=VIEW).status_code == 200
        frames = _frames_until(ws, "channel_created")
        assert frames[-1]["channel"]["id"] == general
        assert frames[-1]["channel"]["private"] is True

        assert overwrite(crew.owner_client, member_path, allow=VIEW, deny=SEND).status_code == 200
        frames = _frames_until(ws, "permissions_updated")
        assert frames[-1]["channels"] == {
            str(general): str(int(MEMBER_PERMISSIONS) & ~SEND)}


def test_permissions_init_carries_the_differing_masks_of_visible_channels(crew):
    path = f"/channels/{crew.general['id']}/permissions/members/{crew.member['id']}"
    assert overwrite(crew.owner_client, path, deny=SEND).status_code == 200
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws.receive_json()
        init = ws.receive_json()
    assert init["type"] == "permissions_init"
    # The private channel is left out: its id must not reach the member.
    assert init["channels"] == {str(crew.sid): {
        str(crew.general["id"]): str(int(MEMBER_PERMISSIONS) & ~SEND)}}


def test_a_private_category_hides_its_channels_until_one_moves_out(crew):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    channel = owner.post(f"/channels/{sid}/create", json={
        "name": "plans", "group_id": group["id"]}).json()
    path = f"/channel-groups/{group['id']}/permissions/roles/{crew.everyone['id']}"
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert overwrite(owner, path, deny=VIEW).status_code == 200
        frames = _frames_until(ws, "channel_group_deleted")
        assert [f["type"] for f in frames] == ["channel_deleted", "channel_group_deleted"]
        assert group["id"] not in [g["id"] for g in frames[-1]["layout"]["groups"]]

        snapshot = crew.member_client.get(f"/servers/{sid}/channels").json()
        assert group["id"] not in [g["id"] for g in snapshot["groups"]]
        assert channel["id"] not in [c["id"] for c in snapshot["channels"]]

        assert owner.patch(f"/channels/{channel['id']}", json={"group_id": None}).status_code == 200
        frames = _frames_until(ws, "channel_created")
        assert frames[-1]["channel"]["id"] == channel["id"]

    staff = owner.get(f"/servers/{sid}/channels").json()["groups"]
    assert [g["private"] for g in staff if g["id"] == group["id"]] == [True]


def test_a_new_channel_in_a_private_category_goes_only_to_its_viewers(crew):
    owner, sid = crew.owner_client, crew.sid
    group = owner.post(f"/servers/{sid}/channel-groups", json={"name": "Staff"}).json()
    path = f"/channel-groups/{group['id']}/permissions/roles/{crew.everyone['id']}"
    assert overwrite(owner, path, deny=VIEW).status_code == 200
    with crew.member_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        created = owner.post(f"/channels/{sid}/create", json={
            "name": "hidden", "group_id": group["id"]}).json()
        assert created["private"] is True
        marker = owner.post(f"/channels/{sid}/create", json={"name": "open"}).json()
        frame = ws.receive_json()
        assert frame["type"] == "channel_created" and frame["channel"]["id"] == marker["id"]


def test_reordering_without_seeing_every_channel_keeps_the_hidden_ones(crew):
    owner, sid, secret = crew.owner_client, crew.sid, crew.secret["id"]
    admin = roles_by_name(owner, sid)["Admin"]
    set_roles(owner, sid, crew.member["id"], [admin["id"]])
    layout = crew.member_client.get(f"/servers/{sid}/channels").json()
    visible = [c["id"] for c in layout["channels"]]
    assert secret not in visible
    groups = [{"id": g["id"], "channel_ids": []} for g in layout["groups"]]
    res = crew.member_client.put(f"/servers/{sid}/channel-layout", json={
        "ungrouped": list(reversed(visible)), "groups": groups})
    assert res.status_code == 200, res.text
    assert res.json()["ungrouped"] == list(reversed(visible))
    full = owner.get(f"/servers/{sid}/channels").json()["channels"]
    assert [c["id"] for c in full] == list(reversed(visible)) + [secret]


# -- cleanup -------------------------------------------------------------------------

def test_kick_removes_the_members_rows_and_role_delete_cascades(crew):
    owner, sid = crew.owner_client, crew.sid
    path = f"/channels/{crew.secret['id']}/permissions/members/{crew.member['id']}"
    assert overwrite(owner, path, allow=VIEW).status_code == 200
    assert owner.delete(f"/servers/{sid}/members/{crew.member['id']}").status_code == 204
    assert run(owner, PermissionOverwrite.filter(user_id=crew.member["id"]).count) == 0

    assert owner.delete(f"/servers/{sid}/roles/{crew.mod_role['id']}").status_code == 204
    assert run(owner, PermissionOverwrite.filter(role_id=crew.mod_role["id"]).count) == 0
    assert crew.mod_client.get(f"/channels/{crew.secret['id']}/messages").status_code == 404


def test_the_owner_sees_everything(crew):
    path = f"/channels/{crew.secret['id']}/permissions/roles/{crew.mod_role['id']}"
    assert crew.owner_client.delete(path).status_code == 204

    async def owner_mask():
        return await permissions.effective(
            crew.owner["id"], crew.sid, channel_id=crew.secret["id"])

    assert run(crew.owner_client, owner_mask) == ALL_PERMISSIONS
    assert crew.owner_client.get(f"/channels/{crew.secret['id']}/messages").status_code == 200
