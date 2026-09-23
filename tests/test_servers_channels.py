from tests.conftest import create_channel, create_server, register


def _join(owner_client, joiner_client, server_id):
    """Add joiner to server_id through the real invite flow."""
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text
    return invite.json()


def test_creator_is_member_of_new_server(client):
    me = register(client)
    server = create_server(client, "Gaming")
    assert [m["id"] for m in server["members"]] == [me["id"]]

    mine = client.get("/servers/me").json()
    assert [s["id"] for s in mine] == [server["id"]]


def test_servers_require_auth(client):
    assert client.post("/servers/", json={"name": "x"}).status_code == 401
    assert client.get("/servers/me").status_code == 401


def test_non_member_cannot_modify_server(client, new_client):
    register(client)
    server = create_server(client)

    outsider = new_client()
    register(outsider)
    assert outsider.put(f"/servers/{server['id']}", json={"name": "pwned"}).status_code == 403
    assert outsider.delete(f"/servers/{server['id']}").status_code == 403


def test_create_channel_appends_to_channel_order(client):
    register(client)
    server = create_server(client)
    first = create_channel(client, server["id"], "general")
    second = create_channel(client, server["id"], "voice", "voice")

    channels = client.get(f"/channels/{server['id']}").json()
    assert {c["id"] for c in channels} == {first["id"], second["id"]}
    assert second["type"] == "voice"

    settings = client.get(f"/servers/{server['id']}").json()["server_settings"]
    assert settings["channel_order"] == [first["id"], second["id"]]


def test_non_member_cannot_list_or_create_channels(client, new_client):
    register(client)
    server = create_server(client)

    outsider = new_client()
    register(outsider)
    assert outsider.get(f"/channels/{server['id']}").status_code == 403
    res = outsider.post(f"/channels/{server['id']}/create", params={"channel_name": "x"})
    assert res.status_code == 403


def test_unknown_server_is_404(client):
    register(client)
    assert client.get("/channels/9999").status_code == 404
    assert client.put("/servers/9999", json={"name": "x"}).status_code == 404


# --- ZET-9: server reads are scoped to membership -------------------------

def test_get_servers_only_lists_own_servers(client, new_client):
    register(client, "alice")
    a_server = create_server(client, "Alice's")

    bob = new_client()
    register(bob, "bob")
    create_server(bob, "Bob's")

    alice_ids = {s["id"] for s in client.get("/servers/").json()}
    assert alice_ids == {a_server["id"]}


def test_non_member_gets_404_on_get_server(client, new_client):
    register(client, "alice")
    server = create_server(client)

    outsider = new_client()
    register(outsider, "bob")
    # 404, not 403: don't leak that the server exists.
    assert outsider.get(f"/servers/{server['id']}").status_code == 404


def test_member_gets_200_on_get_server(client):
    register(client)
    server = create_server(client)
    assert client.get(f"/servers/{server['id']}").status_code == 200


# --- ZET-8: server ownership + owner-only writes --------------------------

def test_create_server_sets_owner_to_creator(client):
    me = register(client)
    server = create_server(client)
    assert server["owner_id"] == me["id"]


def test_non_owner_member_cannot_write_server(client, new_client):
    register(client, "alice")
    server = create_server(client)

    bob = new_client()
    register(bob, "bob")
    _join(client, bob, server["id"])

    # bob is a member but not the owner
    assert bob.put(f"/servers/{server['id']}", json={"name": "pwned"}).status_code == 403
    assert bob.delete(f"/servers/{server['id']}").status_code == 403


def test_owner_can_write_server(client):
    register(client, "alice")
    server = create_server(client)
    assert client.put(f"/servers/{server['id']}", json={"name": "renamed"}).status_code == 200
    assert client.delete(f"/servers/{server['id']}").status_code == 204


def test_non_owner_member_cannot_modify_invites(client, new_client):
    register(client, "alice")
    server = create_server(client)

    bob = new_client()
    register(bob, "bob")
    invite = _join(client, bob, server["id"])

    assert bob.put(f"/invites/{invite['id']}", json={"is_active": False}).status_code == 403
    assert bob.delete(f"/invites/{invite['id']}").status_code == 403


def test_owner_can_modify_invites(client):
    register(client, "alice")
    server = create_server(client)
    invite = client.post("/invites/", json={"server_id": server["id"]}).json()

    assert client.put(f"/invites/{invite['id']}", json={"is_active": False}).status_code == 200
    assert client.delete(f"/invites/{invite['id']}").status_code == 204


def test_non_member_can_preview_server_through_invite(client, new_client):
    register(client, "alice")
    server = create_server(client, "Gaming")
    invite = client.post("/invites/", json={"server_id": server["id"]}).json()

    bob = new_client()
    register(bob, "bob")
    # The server itself stays hidden from non-members...
    assert bob.get(f"/servers/{server['id']}").status_code == 404
    # ...but the invite carries enough to render the join page.
    preview = bob.get(f"/invites/{invite['id']}")
    assert preview.status_code == 200, preview.text
    assert preview.json()["server_name"] == "Gaming"
    assert preview.json()["is_valid"] is True
