"""Channel create (JSON body), update and delete."""
import uuid
from datetime import datetime, timezone

from tests.conftest import ORIGIN, create_channel, create_server, register

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}


def _join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def _post_message(ws, server_id, channel_id):
    message_id = str(uuid.uuid4())
    ws.send_json({
        "type": "message",
        "id": message_id,
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return message_id


# --- create ---------------------------------------------------------------

def test_create_takes_json_body_with_topic(client):
    register(client)
    server = create_server(client)
    res = client.post(f"/channels/{server['id']}/create",
                      json={"name": "  general  ", "topic": "Say hi"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "general"
    assert body["type"] == "text"
    assert body["topic"] == "Say hi"


def test_create_rejects_blank_name_and_unknown_type(client):
    register(client)
    server = create_server(client)
    assert client.post(f"/channels/{server['id']}/create",
                       json={"name": "   "}).status_code == 422
    assert client.post(f"/channels/{server['id']}/create",
                       json={"name": "x", "type": "video"}).status_code == 422
    assert client.post(f"/channels/{server['id']}/create",
                       json={"name": "x" * 101}).status_code == 422


def test_create_no_longer_accepts_query_params(client):
    register(client)
    server = create_server(client)
    res = client.post(f"/channels/{server['id']}/create", params={"channel_name": "x"})
    assert res.status_code == 422


# --- update ---------------------------------------------------------------

def test_owner_can_rename_and_set_topic(client):
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "general")

    res = client.patch(f"/channels/{channel['id']}",
                       json={"name": "lobby", "topic": "Welcome"})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "lobby"
    assert res.json()["topic"] == "Welcome"

    listed = {c["id"]: c for c in client.get(f"/channels/{server['id']}").json()}
    assert listed[channel["id"]]["name"] == "lobby"
    assert listed[channel["id"]]["topic"] == "Welcome"


def test_partial_update_leaves_other_fields(client):
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "general")
    client.patch(f"/channels/{channel['id']}", json={"topic": "First"})

    res = client.patch(f"/channels/{channel['id']}", json={"name": "renamed"})
    assert res.json()["topic"] == "First"

    # An empty topic clears it.
    res = client.patch(f"/channels/{channel['id']}", json={"topic": "  "})
    assert res.json()["topic"] is None
    assert res.json()["name"] == "renamed"


def test_update_validates_name(client):
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"])
    assert client.patch(f"/channels/{channel['id']}", json={"name": ""}).status_code == 422
    assert client.patch(f"/channels/{channel['id']}", json={"name": None}).status_code == 422
    assert client.patch(f"/channels/{channel['id']}",
                        json={"topic": "x" * 1025}).status_code == 422


def test_channel_type_is_locked(client):
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "general", "text")

    assert client.patch(f"/channels/{channel['id']}", json={"type": "voice"}).status_code == 400
    # Echoing the current type back is fine.
    assert client.patch(f"/channels/{channel['id']}",
                        json={"type": "text", "name": "g"}).status_code == 200


def test_non_owner_cannot_update_or_delete(client, new_client):
    register(client, "alice-ch")
    server = create_server(client)
    channel = create_channel(client, server["id"])

    bob = new_client()
    register(bob, "bob-ch")
    _join(client, bob, server["id"])
    assert bob.patch(f"/channels/{channel['id']}", json={"name": "pwned"}).status_code == 403
    assert bob.delete(f"/channels/{channel['id']}").status_code == 403

    outsider = new_client()
    register(outsider, "eve-ch")
    assert outsider.patch(f"/channels/{channel['id']}", json={"name": "x"}).status_code == 403
    assert outsider.delete(f"/channels/{channel['id']}").status_code == 403


def test_unknown_channel_is_404(client):
    register(client)
    assert client.patch("/channels/9999", json={"name": "x"}).status_code == 404
    assert client.delete("/channels/9999").status_code == 404


# --- delete ---------------------------------------------------------------

def test_delete_removes_channel_messages_and_order(client):
    register(client)
    server = create_server(client)
    keep = create_channel(client, server["id"], "keep")
    doomed = create_channel(client, server["id"], "doomed")

    # A second socket of the same user receives each message once it's stored,
    # which is what makes the follow-up reads deterministic.
    with client.websocket_connect("/ws", headers=HEADERS) as ws, \
            client.websocket_connect("/ws", headers=HEADERS) as echo:
        ws.receive_json()    # presence_init
        echo.receive_json()  # presence_init
        for channel in (doomed, keep):
            mid = _post_message(ws, server["id"], channel["id"])
            assert echo.receive_json()["id"] == mid

    assert len(client.get(f"/channels/{doomed['id']}/messages").json()["messages"]) == 1
    assert len(client.get(f"/channels/{keep['id']}/messages").json()["messages"]) == 1

    assert client.delete(f"/channels/{doomed['id']}").status_code == 204

    assert [c["id"] for c in client.get(f"/channels/{server['id']}").json()] == [keep["id"]]
    assert client.get(f"/channels/{doomed['id']}/messages").status_code == 404
    assert len(client.get(f"/channels/{keep['id']}/messages").json()["messages"]) == 1

    settings = client.get(f"/servers/{server['id']}").json()["server_settings"]
    assert settings["channel_order"] == [keep["id"]]


# --- realtime -------------------------------------------------------------

def test_update_and_delete_broadcast_to_other_members(client, new_client):
    register(client, "alice-cb")
    server = create_server(client)
    channel = create_channel(client, server["id"], "general")
    bob = new_client()
    register(bob, "bob-cb")
    _join(client, bob, server["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()  # presence_init
        bob_ws.receive_json()    # presence_init
        alice_ws.receive_json()  # bob online

        client.patch(f"/channels/{channel['id']}", json={"name": "lobby", "topic": "hey"})
        assert bob_ws.receive_json() == {
            "type": "channel_updated",
            "server_id": server["id"],
            "channel": {
                "id": channel["id"],
                "name": "lobby",
                "channel_settings": {},
                "type": "text",
                "topic": "hey",
            },
        }

        client.delete(f"/channels/{channel['id']}")
        assert bob_ws.receive_json() == {
            "type": "channel_deleted",
            "server_id": server["id"],
            "channel_id": channel["id"],
        }

        # The owner is excluded from both: her next frame is bob's message.
        other = create_channel(client, server["id"], "other")
        bob_ws.receive_json()  # channel_created
        _post_message(bob_ws, server["id"], other["id"])
        assert alice_ws.receive_json()["type"] == "message"
