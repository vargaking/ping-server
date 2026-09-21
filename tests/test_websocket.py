"""ZET-5: the WebSocket identity comes from the session cookie, never the client."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import ORIGIN, PREVIEW_ORIGIN, create_channel, create_server, register

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}


def chat_frame(server_id, channel_id, **extra):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def join(owner_client, joiner_client, server_id):
    """Put joiner into the server through the real invite flow."""
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


@pytest.fixture
def two_members(client, new_client):
    """alice (owner) and bob, both members of one server with one text channel."""
    alice = register(client, "alice")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    bob = register(bob_client, "bob")
    join(client, bob_client, server["id"])
    return client, alice, bob_client, bob, server, channel


# --- handshake -------------------------------------------------------------

def test_unauthenticated_connect_is_closed_with_4401(client):
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_invalid_cookie_is_closed_with_4401(client):
    client.cookies.set("access_token", "forged")
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_foreign_origin_is_rejected_even_with_valid_cookie(client):
    register(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass


def test_vercel_preview_origin_is_allowed_by_regex(client):
    # ZET-59: preview subdomains match ALLOWED_ORIGIN_REGEX, so /ws accepts them
    # just like an explicitly-allowlisted origin would be.
    register(client)
    with client.websocket_connect("/ws", headers={"origin": PREVIEW_ORIGIN}) as ws:
        assert ws.receive_json()["type"] == "presence_init"


def test_foreign_vercel_origin_is_rejected(client):
    # A different team's Vercel app must not slip through the regex.
    register(client)
    foreign = "https://ping-frontend-abc123-someone-else-projects.vercel.app"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": foreign}):
            pass


def test_no_origin_header_is_allowed_for_non_browser_clients(client):
    register(client)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "presence_init"


def test_authenticated_connect_gets_presence_without_connection_init(client):
    register(client)
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        assert ws.receive_json() == {"type": "presence_init", "user_ids": []}


# --- presence --------------------------------------------------------------

def test_presence_flows_between_members(two_members):
    alice_client, alice, bob_client, bob, *_ = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        assert alice_ws.receive_json() == {"type": "presence_init", "user_ids": []}

        with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
            assert bob_ws.receive_json() == {"type": "presence_init", "user_ids": [alice["id"]]}
            assert alice_ws.receive_json() == {
                "type": "presence_update", "user_id": bob["id"], "online": True}

        assert alice_ws.receive_json() == {
            "type": "presence_update", "user_id": bob["id"], "online": False}


def test_legacy_connection_init_cannot_claim_another_identity(two_members):
    alice_client, alice, bob_client, bob, *_ = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        alice_ws.receive_json()
        with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
            bob_ws.receive_json()
            alice_ws.receive_json()  # bob online

            # bob's (old or malicious) client claims to be alice
            bob_ws.send_json({"type": "connection_init", "user_id": alice["id"]})
            # he just gets his own presence snapshot again...
            assert bob_ws.receive_json() == {"type": "presence_init", "user_ids": [alice["id"]]}

            # ...and alice's socket is still hers: bob's message reaches her
            _, _, _, _, server, channel = two_members
            bob_ws.send_json(chat_frame(server["id"], channel["id"]))
            assert alice_ws.receive_json()["user_id"] == bob["id"]


# --- messages --------------------------------------------------------------

def test_forged_user_id_is_attributed_to_real_sender(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json(); bob_ws.receive_json(); alice_ws.receive_json()

        frame = chat_frame(server["id"], channel["id"], user_id=alice["id"], is_admin=True)
        bob_ws.send_json(frame)

        delivered = alice_ws.receive_json()
        assert delivered["user_id"] == bob["id"]
        assert delivered["id"] == frame["id"]
        assert delivered["content"] == DOC
        assert "is_admin" not in delivered  # unknown fields are not relayed

    stored = alice_client.get("/channels/messages", params={"last_updated": "1970-01-01T00:00:00Z"}).json()
    assert [(m["id"], m["user_id"]) for m in stored] == [(frame["id"], bob["id"])]
    assert json.loads(stored[0]["content"]) == DOC


def test_frame_without_user_id_works(two_members):
    """The new frontend stops sending user_id altogether."""
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json(); bob_ws.receive_json(); alice_ws.receive_json()
        alice_ws.send_json(chat_frame(server["id"], channel["id"]))
        assert bob_ws.receive_json()["user_id"] == alice["id"]


def test_non_member_cannot_post_into_a_server(two_members, new_client):
    alice_client, alice, _, _, server, channel = two_members
    mallory_client = new_client()
    register(mallory_client, "mallory")

    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            mallory_client.websocket_connect("/ws", headers=HEADERS) as mallory_ws:
        alice_ws.receive_json(); mallory_ws.receive_json()

        frame = chat_frame(server["id"], channel["id"], user_id=alice["id"])
        mallory_ws.send_json(frame)
        assert mallory_ws.receive_json() == {"type": "error", "code": "forbidden", "ref": frame["id"]}

    stored = alice_client.get("/channels/messages", params={"last_updated": "1970-01-01T00:00:00Z"}).json()
    assert stored == []


def test_channel_must_belong_to_the_server(two_members):
    alice_client, *_ , server, channel = two_members
    other_server = create_server(alice_client, "Other")
    with alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws.receive_json()
        frame = chat_frame(other_server["id"], channel["id"])
        ws.send_json(frame)
        assert ws.receive_json()["code"] == "forbidden"


# --- ZET-10: frame validation ---------------------------------------------

def test_malformed_frame_yields_error_and_keeps_socket_open(client):
    register(client)
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        assert ws.receive_json()["type"] == "presence_init"

        # unknown frame type
        ws.send_json({"type": "nonsense"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_frame", "ref": None}

        # message frame missing required fields (server_id, channel_id, ...)
        ws.send_json({"type": "message", "id": "abc"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_frame", "ref": "abc"}

        # a non-object frame
        ws.send_json([1, 2, 3])
        assert ws.receive_json() == {"type": "error", "code": "invalid_frame", "ref": None}

        # the socket is still alive and processing frames afterwards
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json() == {"type": "presence_init", "user_ids": []}


def test_disconnect_clears_connection_state(client):
    from app.app import comms
    me = register(client)
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws.receive_json()
        assert comms.connection_manager.is_online(me["id"])
    assert not comms.connection_manager.is_online(me["id"])
