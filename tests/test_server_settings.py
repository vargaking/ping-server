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


def _put_icon(client, server_id, **body):
    return client.put(f"/servers/{server_id}", json=body)


def test_text_icon_is_saved_and_broadcast(client, new_client):
    register(client, "alice-ti")
    server = create_server(client)
    bob = new_client()
    register(bob, "bob-ti")
    _join(client, bob, server["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        res = _put_icon(client, server["id"], icon_text="ÁB", icon_tone=4)
        assert res.status_code == 200, res.text
        assert (res.json()["icon_text"], res.json()["icon_tone"]) == ("ÁB", 4)

        frame = bob_ws.receive_json()
        assert frame["type"] == "server_updated"
        assert (frame["server"]["icon_text"], frame["server"]["icon_tone"]) == ("ÁB", 4)

    fetched = client.get(f"/servers/{server['id']}").json()
    assert (fetched["icon_text"], fetched["icon_tone"]) == ("ÁB", 4)
    assert client.get("/servers/me").json()[0]["icon_text"] == "ÁB"


def test_text_icon_normalises_to_nfc(client):
    register(client)
    server = create_server(client)
    res = _put_icon(client, server["id"], icon_text=" ÁB ")
    assert res.status_code == 200, res.text
    assert res.json()["icon_text"] == "ÁB"


def test_text_icon_accepts_emoji_sequences(client):
    register(client)
    server = create_server(client)
    for text in ("😀", "👨‍👩‍👧", "🇭🇺", "👨‍👩‍👧🇭🇺"):
        res = _put_icon(client, server["id"], icon_text=text, icon_tone=1)
        assert res.status_code == 200, (text, res.text)
        assert res.json()["icon_text"] == text


def test_text_icon_rejects_bad_text(client):
    register(client)
    server = create_server(client)
    _put_icon(client, server["id"], icon_text="OK")
    for bad in ("ABC", "A B", "😀😀😀", "x" * 33, "🇭🇺" * 17, "A\x00", "A\tB"):
        res = _put_icon(client, server["id"], icon_text=bad)
        assert res.status_code == 422, (bad, res.text)
    assert client.get(f"/servers/{server['id']}").json()["icon_text"] == "OK"


def test_text_icon_error_message_is_plain(client):
    register(client)
    server = create_server(client)
    res = _put_icon(client, server["id"], icon_text="ABC")
    assert "at most 2 characters" in res.json()["detail"][0]["msg"]


def test_icon_tone_range(client):
    register(client)
    server = create_server(client)
    for bad in (0, 7, "x"):
        assert _put_icon(client, server["id"], icon_tone=bad).status_code == 422
    for tone in range(1, 7):
        res = _put_icon(client, server["id"], icon_tone=tone)
        assert res.status_code == 200, res.text
        assert res.json()["icon_tone"] == tone


def test_text_icon_without_tone_defaults_to_first(client):
    register(client)
    server = create_server(client)
    res = _put_icon(client, server["id"], icon_text="AB")
    assert res.json()["icon_tone"] == 1


def test_null_or_blank_text_clears_icon(client):
    register(client)
    server = create_server(client)
    _put_icon(client, server["id"], icon_text="AB", icon_tone=2)
    assert _put_icon(client, server["id"], icon_text=None).json()["icon_text"] is None
    _put_icon(client, server["id"], icon_text="AB")
    assert _put_icon(client, server["id"], icon_text="  ").json()["icon_text"] is None
    assert _put_icon(client, server["id"], icon_tone=None).json()["icon_tone"] is None


def test_icon_changes_need_manage_server(client, new_client):
    register(client, "alice-pm")
    server = create_server(client)
    bob = new_client()
    register(bob, "bob-pm")
    _join(client, bob, server["id"])

    assert _put_icon(bob, server["id"], icon_text="AB").status_code == 403
    assert bob.delete(f"/servers/{server['id']}/icon").status_code == 403
    assert client.get(f"/servers/{server['id']}").json()["icon_text"] is None


def test_delete_image_keeps_text_icon(client):
    register(client)
    server = create_server(client)
    _put_icon(client, server["id"], icon_text="AB", icon_tone=3)
    client.post(f"/servers/{server['id']}/icon",
                files={"file": ("icon.png", PNG_1PX, "image/png")})

    res = client.delete(f"/servers/{server['id']}/icon")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "icon" not in body["server_profile"]
    assert (body["icon_text"], body["icon_tone"]) == ("AB", 3)
    fetched = client.get(f"/servers/{server['id']}").json()
    assert "icon" not in fetched["server_profile"]
    assert client.delete(f"/servers/{server['id']}/icon").status_code == 200
