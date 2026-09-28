"""A profile change (username or avatar) is announced with a ``user_invalidate``
frame to every member who shares a server with the user, and to all of the
user's own sockets so that their other tabs refresh as well. Users who share no
server with them are not told.
"""
from contextlib import ExitStack
from io import BytesIO

from PIL import Image

from tests.conftest import ORIGIN, create_server, register
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _open_sockets(stack, alice_client, bob_client):
    """Connect Bob and two Alice sockets (two tabs) and drain the connect-time
    frames, so the next frame on each socket is whatever the test triggers."""
    bob_ws = stack.enter_context(bob_client.websocket_connect("/ws", headers=HEADERS))
    assert bob_ws.receive_json()["type"] == "presence_init"

    alice_ws1 = stack.enter_context(alice_client.websocket_connect("/ws", headers=HEADERS))
    assert alice_ws1.receive_json()["type"] == "presence_init"
    assert bob_ws.receive_json()["type"] == "presence_update"  # alice came online

    # A second tab only gets its own snapshot; alice was already online.
    alice_ws2 = stack.enter_context(alice_client.websocket_connect("/ws", headers=HEADERS))
    assert alice_ws2.receive_json()["type"] == "presence_init"
    return bob_ws, alice_ws1, alice_ws2


def _setup(client, new_client, suffix):
    alice = register(client, f"alice-{suffix}")
    server = create_server(client)
    bob_client = new_client()
    register(bob_client, f"bob-{suffix}")
    invite_and_join(client, bob_client, server["id"])
    return alice, bob_client


def test_username_change_reaches_members_and_own_tabs(client, new_client):
    alice, bob_client = _setup(client, new_client, "un")

    with ExitStack() as stack:
        bob_ws, alice_ws1, alice_ws2 = _open_sockets(stack, client, bob_client)

        res = client.put(f"/users/{alice['id']}", json={"username": "alice-renamed"})
        assert res.status_code == 200, res.text

        expected = {"type": "user_invalidate", "user_id": alice["id"]}
        assert bob_ws.receive_json() == expected
        assert alice_ws1.receive_json() == expected
        assert alice_ws2.receive_json() == expected


def test_avatar_upload_reaches_members_and_own_tabs(client, new_client):
    alice, bob_client = _setup(client, new_client, "av")

    with ExitStack() as stack:
        bob_ws, alice_ws1, alice_ws2 = _open_sockets(stack, client, bob_client)

        files = {"file": ("avatar.png", _png_bytes(), "image/png")}
        res = client.post(f"/users/{alice['id']}/avatar", files=files)
        assert res.status_code == 200, res.text

        expected = {"type": "user_invalidate", "user_id": alice["id"]}
        assert bob_ws.receive_json() == expected
        assert alice_ws1.receive_json() == expected
        assert alice_ws2.receive_json() == expected


def test_user_without_shared_server_is_not_notified(client, new_client):
    alice = register(client, "alice-ns")
    create_server(client)
    carol_client = new_client()
    register(carol_client, "carol-ns")

    with carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        assert carol_ws.receive_json()["type"] == "presence_init"

        res = client.put(f"/users/{alice['id']}", json={"username": "alice-ns-2"})
        assert res.status_code == 200, res.text

        # Frames arrive in order, so if a user_invalidate had been queued for
        # Carol it would come before the reply to this legacy snapshot request.
        carol_ws.send_json({"type": "connection_init"})
        assert carol_ws.receive_json()["type"] == "presence_init"
