"""A profile change (username, avatar or banner) is announced with a ``user_invalidate``
frame to every member who shares a server with the user, to everyone who has a
direct conversation with them, and to all of the user's own sockets so that their
other tabs refresh as well. Other users are not told.
"""
from contextlib import ExitStack
from io import BytesIO

import pytest
from PIL import Image

from tests.conftest import ORIGIN, create_server, register, ws_ready
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
    ws_ready(bob_ws)

    alice_ws1 = stack.enter_context(alice_client.websocket_connect("/ws", headers=HEADERS))
    ws_ready(alice_ws1)
    assert bob_ws.receive_json()["type"] == "presence_update"  # alice came online

    # A second tab only gets its own snapshot; alice was already online.
    alice_ws2 = stack.enter_context(alice_client.websocket_connect("/ws", headers=HEADERS))
    ws_ready(alice_ws2)
    return bob_ws, alice_ws1, alice_ws2


def _setup(client, new_client, suffix):
    alice = register(client, f"alice-{suffix}")
    server = create_server(client)
    bob_client = new_client()
    register(bob_client, f"bob-{suffix}")
    invite_and_join(client, bob_client, server["id"])
    return alice, bob_client


def _put_username(client, uid):
    return client.put(f"/users/{uid}", json={"username": "alice-renamed"})


def _post_avatar(client, uid):
    return client.post(f"/users/{uid}/avatar", files={"file": ("a.png", _png_bytes(), "image/png")})


def _put_banner_color(client, uid):
    return client.put(f"/users/{uid}", json={"banner_color": "#112233"})


def _post_banner(client, uid):
    return client.post(f"/users/{uid}/banner", files={"file": ("b.png", _png_bytes(), "image/png")})


def _delete_banner(client, uid):
    return client.delete(f"/users/{uid}/banner")


@pytest.mark.parametrize(
    "trigger",
    [_put_username, _post_avatar, _put_banner_color, _post_banner, _delete_banner],
    ids=["put_username", "post_avatar", "put_banner_color", "post_banner", "delete_banner"],
)
def test_profile_change_reaches_members_and_own_tabs(client, new_client, trigger):
    alice, bob_client = _setup(client, new_client, "pc")

    with ExitStack() as stack:
        bob_ws, alice_ws1, alice_ws2 = _open_sockets(stack, client, bob_client)

        res = trigger(client, alice["id"])
        assert res.status_code == 200, res.text

        expected = {"type": "user_invalidate", "user_id": alice["id"]}
        assert bob_ws.receive_json() == expected
        assert alice_ws1.receive_json() == expected
        assert alice_ws2.receive_json() == expected


@pytest.mark.parametrize("has_conversation", [True, False])
def test_reach_without_shared_server(client, new_client, has_conversation):
    alice = register(client, "alice-ns")
    create_server(client)
    carol_client = new_client()
    register(carol_client, "carol-ns")
    if has_conversation:
        res = carol_client.post("/conversations/", json={"user_id": alice["id"]})
        assert res.status_code == 200, res.text

    with carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        ws_ready(carol_ws)

        res = client.put(f"/users/{alice['id']}", json={"username": "alice-ns-2"})
        assert res.status_code == 200, res.text

        if has_conversation:
            assert carol_ws.receive_json() == {"type": "user_invalidate", "user_id": alice["id"]}
        else:
            # Frames arrive in order, so if a user_invalidate had been queued for
            # Carol it would come before the reply to this legacy snapshot request.
            carol_ws.send_json({"type": "connection_init"})
            assert carol_ws.receive_json()["type"] == "presence_init"
