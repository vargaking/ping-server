from tests.conftest import create_channel, create_server, register


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
