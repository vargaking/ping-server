"""Sends to a socket that stopped taking frames are bounded, and the socket is
dropped: forgotten at once, its user announced offline, the socket closed in
the background."""
import asyncio

import pytest

from app.communication import WS_CLOSE_STALLED
from app.services import socket_send
from app.services.socket_send import (
    accept_within_timeout,
    close_quietly,
    send_frame,
    send_frame_to_all,
)
from tests.conftest import create_channel, create_server, register, ws_ready
from tests.test_websocket import HEADERS, chat_frame, join


class FakeSocket:
    def __init__(self, *, hang_send=False, hang_close=False, hang_accept=False, error=None):
        self.hang_send = hang_send
        self.hang_close = hang_close
        self.hang_accept = hang_accept
        self.error = error
        self.sent = []
        self.close_codes = []

    async def send_json(self, frame):
        if self.error:
            raise self.error
        if self.hang_send:
            await asyncio.Event().wait()
        self.sent.append(frame)

    async def close(self, code=1000, reason=None):
        self.close_codes.append(code)
        if self.hang_close:
            await asyncio.Event().wait()

    async def accept(self):
        if self.hang_accept:
            await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def short_timeouts(monkeypatch):
    monkeypatch.setattr(socket_send, "SEND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(socket_send, "CLOSE_TIMEOUT_SECONDS", 0.05)


# The helpers on their own

def test_send_frame_delivers_and_reports_success():
    socket = FakeSocket()
    stalled = []

    assert asyncio.run(send_frame(socket, {"type": "a"}, stalled.append)) is True
    assert socket.sent == [{"type": "a"}]
    assert stalled == []


def test_send_frame_gives_up_on_a_stalled_socket_and_hands_it_over():
    socket = FakeSocket(hang_send=True)
    stalled = []

    assert asyncio.run(send_frame(socket, {"type": "a"}, stalled.append)) is False
    assert stalled == [socket]


def test_send_frame_without_a_handler_just_returns_false():
    assert asyncio.run(send_frame(FakeSocket(hang_send=True), {"type": "a"})) is False


@pytest.mark.parametrize("error", [RuntimeError("closed"), ConnectionResetError()])
def test_other_send_failures_are_skipped_without_dropping(error):
    socket = FakeSocket(error=error)
    stalled = []

    assert asyncio.run(send_frame(socket, {"type": "a"}, stalled.append)) is False
    assert stalled == []


def test_one_stalled_socket_does_not_stop_the_rest():
    healthy_a, healthy_b = FakeSocket(), FakeSocket()
    broken = FakeSocket(error=RuntimeError("closed"))
    stalled_socket = FakeSocket(hang_send=True)
    stalled = []

    asyncio.run(send_frame_to_all(
        [healthy_a, stalled_socket, broken, healthy_b], {"type": "a"}, stalled.append))

    assert healthy_a.sent == healthy_b.sent == [{"type": "a"}]
    assert stalled == [stalled_socket]


def test_frames_to_one_socket_stay_in_order_around_a_stalled_one():
    healthy, stuck = FakeSocket(), FakeSocket(hang_send=True)

    async def scenario():
        for n in (1, 2, 3):
            await send_frame_to_all([stuck, healthy], {"n": n})

    asyncio.run(scenario())
    assert healthy.sent == [{"n": 1}, {"n": 2}, {"n": 3}]


@pytest.mark.parametrize("hang", [False, True])
def test_close_quietly_is_bounded_and_never_raises(hang):
    socket = FakeSocket(hang_close=hang)

    asyncio.run(close_quietly(socket, 4000))

    assert socket.close_codes == [4000]


def test_close_quietly_ignores_a_socket_that_is_already_gone():
    class Gone(FakeSocket):
        async def close(self, code=1000, reason=None):
            raise RuntimeError("already closed")

    asyncio.run(close_quietly(Gone()))


def test_accept_that_never_finishes_closes_the_socket():
    socket = FakeSocket(hang_accept=True)

    assert asyncio.run(accept_within_timeout(socket)) is False
    assert socket.close_codes == [1000]


def test_accept_that_finishes_leaves_the_socket_open():
    socket = FakeSocket()

    assert asyncio.run(accept_within_timeout(socket)) is True
    assert socket.close_codes == []


# Through the app: a stalled socket registered next to real ones

@pytest.fixture
def members(client, new_client):
    """alice (owner, the client that registers the stalled socket), bob and
    carol: three members of one server with one text channel."""
    alice = register(client, "alice")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client, carol_client = new_client(), new_client()
    bob = register(bob_client, "bob")
    carol = register(carol_client, "carol")
    join(client, bob_client, server["id"])
    join(client, carol_client, server["id"])
    return {
        "comms": client.app.state.comms,
        "portal": client.portal,
        "alice": alice, "bob": bob, "carol": carol,
        "bob_client": bob_client, "carol_client": carol_client,
        "server": server, "channel": channel,
    }


def stall(members, socket=None):
    socket = socket or FakeSocket(hang_send=True)
    members["comms"].connection_manager.add_connection(members["alice"]["id"], socket)
    return socket


def alice_went_offline(members):
    return {"type": "presence_update", "user_id": members["alice"]["id"], "online": False}


def settle(members):
    """Wait for the background close and offline announcement of dropped sockets."""
    async def wait():
        await asyncio.gather(*members["comms"]._background)

    members["portal"].call(wait)


def receive(ws, count):
    return [ws.receive_json() for _ in range(count)]


def assert_dropped(members, socket):
    manager = members["comms"].connection_manager
    assert socket not in manager.websocket_to_user
    assert not manager.is_online(members["alice"]["id"])


def probe(n):
    return {"type": "probe", "n": n}


def trigger_send_to_users(members):
    ids = [members["alice"]["id"], members["bob"]["id"]]
    with members["bob_client"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        socket = stall(members)
        for n in (1, 2):
            members["portal"].call(members["comms"].send_to_users, ids, probe(n))
        return socket, receive(bob_ws, 3), [probe(1), probe(2)]


def trigger_fan_out(members):
    server_id, channel_id = members["server"]["id"], members["channel"]["id"]
    with members["bob_client"].websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            members["carol_client"].websocket_connect("/ws", headers=HEADERS) as carol_ws:
        ws_ready(bob_ws)
        ws_ready(carol_ws)
        bob_ws.receive_json()  # carol came online
        socket = stall(members)
        sent = [chat_frame(server_id, channel_id) for _ in range(2)]
        for frame in sent:
            carol_ws.send_json(frame)
        acks = [f for f in receive(carol_ws, 3) if f["type"] == "message_ack"]
        assert [f["id"] for f in acks] == [f["id"] for f in sent]
        got = receive(bob_ws, 3)
        return socket, got, [f["id"] for f in sent]


def trigger_presence_update(members):
    carol = members["carol"]
    with members["bob_client"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        socket = stall(members)
        with members["carol_client"].websocket_connect("/ws", headers=HEADERS) as carol_ws:
            ws_ready(carol_ws)
        online = {"type": "presence_update", "user_id": carol["id"], "online": True}
        offline = {"type": "presence_update", "user_id": carol["id"], "online": False}
        return socket, receive(bob_ws, 3), [online, offline]


@pytest.mark.parametrize("trigger", [
    trigger_send_to_users, trigger_fan_out, trigger_presence_update,
], ids=["send_to_users", "fan_out", "presence_update"])
def test_stalled_socket_is_dropped_and_healthy_ones_still_receive_in_order(members, trigger):
    socket, frames, expected = trigger(members)

    offline = alice_went_offline(members)
    assert frames.count(offline) == 1
    rest = [f for f in frames if f != offline]
    if trigger is trigger_fan_out:
        rest = [f["id"] for f in rest]
    assert rest == expected
    assert_dropped(members, socket)
    assert socket.close_codes == [WS_CLOSE_STALLED]


@pytest.mark.parametrize("single_send", ["pong", "invalid_frame", "permissions_init", "presence_init"])
def test_stalled_single_socket_sends_drop_the_socket(members, single_send):
    comms, alice_id = members["comms"], members["alice"]["id"]
    socket = stall(members)
    sends = {
        "pong": lambda: comms.message_switch({"type": "ping", "t": 1}, socket),
        "invalid_frame": lambda: comms.message_switch({"type": "nonsense"}, socket),
        "permissions_init": lambda: comms._send_permissions_snapshot(alice_id, socket),
        "presence_init": lambda: comms._send_presence_snapshot(alice_id, socket),
    }

    members["portal"].call(sends[single_send])
    settle(members)

    assert_dropped(members, socket)


def test_a_user_with_another_live_socket_is_not_announced_offline(members, client):
    alice_id = members["alice"]["id"]
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            members["bob_client"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob came online
        socket = stall(members)
        ids = [alice_id, members["bob"]["id"]]
        for n in (1, 2):
            members["portal"].call(members["comms"].send_to_users, ids, probe(n))

        assert receive(bob_ws, 2) == [probe(1), probe(2)]
        assert receive(alice_ws, 2) == [probe(1), probe(2)]
        settle(members)
        manager = members["comms"].connection_manager
        assert socket not in manager.websocket_to_user
        assert manager.is_online(alice_id)
    assert socket.close_codes == [WS_CLOSE_STALLED]


def test_dropping_does_not_wait_for_the_close(members):
    comms = members["comms"]
    socket = stall(members, FakeSocket(hang_send=True, hang_close=True))

    async def scenario():
        await comms.send_to_users([members["alice"]["id"]], probe(1))
        registered_after_send = socket in comms.connection_manager.websocket_to_user
        pending_close = bool(comms._background)
        await asyncio.gather(*comms._background)
        return registered_after_send, pending_close, bool(comms._background)

    registered, pending, left_over = members["portal"].call(scenario)

    assert (registered, pending, left_over) == (False, True, False)
    assert socket.close_codes == [WS_CLOSE_STALLED]
