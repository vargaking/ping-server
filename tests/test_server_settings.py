"""Server name validation and the server_updated / server_deleted frames."""
from tests.conftest import ORIGIN, create_server, register, ws_ready

HEADERS = {"origin": ORIGIN}
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def test_rename_trims_and_rejects_blank(client):
    register(client)
    server = create_server(client)

    res = client.put(f"/servers/{server['id']}", json={"name": "  Lobby  "})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Lobby"

    for bad in ("", "   ", None, "x" * 101):
        assert client.put(f"/servers/{server['id']}", json={"name": bad}).status_code == 422
    assert client.get(f"/servers/{server['id']}").json()["name"] == "Lobby"


def test_create_rejects_blank_name(client):
    register(client)
    assert client.post("/servers/", json={"name": "  "}).status_code == 422


def test_rename_and_icon_broadcast_to_other_members(client, new_client):
    register(client, "alice-su")
    server = create_server(client, "Old")
    bob = new_client()
    register(bob, "bob-su")
    _join(client, bob, server["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        client.put(f"/servers/{server['id']}", json={"name": "New"})
        frame = bob_ws.receive_json()
        assert frame["type"] == "server_updated"
        assert frame["server"]["id"] == server["id"]
        assert frame["server"]["name"] == "New"

        res = client.post(f"/servers/{server['id']}/icon",
                          files={"file": ("icon.png", PNG_1PX, "image/png")})
        assert res.status_code == 200, res.text
        frame = bob_ws.receive_json()
        assert frame["type"] == "server_updated"
        assert frame["server"]["server_profile"]["icon"] == res.json()["server_profile"]["icon"]

        client.put(f"/servers/{server['id']}",
                   json={"server_settings": {"channel_order": [3, 1, 2]}})
        frame = bob_ws.receive_json()
        assert frame["type"] == "server_updated"
        assert frame["server"]["server_settings"]["channel_order"] == [3, 1, 2]

        client.delete(f"/servers/{server['id']}")
        assert bob_ws.receive_json() == {"type": "server_deleted", "server_id": server["id"]}
    assert bob.get("/servers/me").json() == []
