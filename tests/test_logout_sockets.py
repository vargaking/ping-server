"""Logging out closes the sockets that authenticated with that session."""
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import ORIGIN, PASSWORD, create_server, register, ws_ready
from tests.test_websocket import join

HEADERS = {"origin": ORIGIN}


def assert_closed_unauthenticated(ws):
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == 4401


@pytest.fixture
def two_members(client, new_client):
    alice = register(client, "alice")
    server = create_server(client)
    bob_client = new_client()
    bob = register(bob_client, "bob")
    join(client, bob_client, server["id"])
    return client, alice, bob_client, bob


def test_logout_closes_every_socket_of_that_session(client):
    register(client)
    with client.websocket_connect("/ws", headers=HEADERS) as first, \
            client.websocket_connect("/ws", headers=HEADERS) as second:
        ws_ready(first)
        ws_ready(second)

        assert client.post("/auth/logout").status_code == 200

        assert_closed_unauthenticated(first)
        assert_closed_unauthenticated(second)


def test_logout_keeps_sockets_of_another_session_open(client, new_client):
    register(client, "alice")
    other = new_client()
    assert other.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200

    with client.websocket_connect("/ws", headers=HEADERS) as closed_ws, \
            other.websocket_connect("/ws", headers=HEADERS) as open_ws:
        ws_ready(closed_ws)
        ws_ready(open_ws)

        assert client.post("/auth/logout").status_code == 200
        assert_closed_unauthenticated(closed_ws)

        other.post("/auth/logout")
        assert_closed_unauthenticated(open_ws)


def test_other_session_socket_still_receives_frames(two_members, new_client):
    alice_client, alice, bob_client, bob = two_members
    alice_again = new_client()
    assert alice_again.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200

    with alice_again.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        with alice_client.websocket_connect("/ws", headers=HEADERS) as old_ws:
            ws_ready(old_ws)
            assert alice_client.post("/auth/logout").status_code == 200
            assert_closed_unauthenticated(old_ws)

        with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
            ws_ready(bob_ws)
            assert alice_ws.receive_json() == {
                "type": "presence_update", "user_id": bob["id"], "online": True}


def test_logout_of_the_only_session_announces_offline(two_members):
    alice_client, alice, bob_client, bob = two_members
    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
            ws_ready(alice_ws)
            assert bob_ws.receive_json() == {
                "type": "presence_update", "user_id": alice["id"], "online": True}

            assert alice_client.post("/auth/logout").status_code == 200

            assert bob_ws.receive_json() == {
                "type": "presence_update", "user_id": alice["id"], "online": False}
            assert_closed_unauthenticated(alice_ws)


def test_logout_of_one_of_two_sessions_does_not_announce_offline(two_members, new_client):
    alice_client, alice, bob_client, bob = two_members
    alice_again = new_client()
    assert alice_again.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        with alice_client.websocket_connect("/ws", headers=HEADERS) as old_ws, \
                alice_again.websocket_connect("/ws", headers=HEADERS) as alice_ws:
            ws_ready(old_ws)
            ws_ready(alice_ws)
            assert bob_ws.receive_json() == {
                "type": "presence_update", "user_id": alice["id"], "online": True}

            assert alice_client.post("/auth/logout").status_code == 200
            assert_closed_unauthenticated(old_ws)

            alice_again.post("/auth/logout")
            assert bob_ws.receive_json() == {
                "type": "presence_update", "user_id": alice["id"], "online": False}
            assert_closed_unauthenticated(alice_ws)


def test_logout_without_cookie_is_ok(client):
    assert client.post("/auth/logout").status_code == 200


def test_logout_with_unknown_cookie_is_ok(client):
    client.cookies.set("access_token", "forged")
    assert client.post("/auth/logout").status_code == 200
