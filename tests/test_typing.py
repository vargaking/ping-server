"""Typing indicators are relayed live to the thread's other participants and
never stored, echoed to the sender's own sockets, or answered with an error."""
import pytest

from app.app import comms
from app.services.chat_service import TYPING_MIN_INTERVAL, ChatService
from tests.conftest import create_server, register, ws_ready
from tests.test_direct_messages import open_conversation
from tests.test_websocket import HEADERS, chat_frame, join, two_members  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_throttle():
    # User and channel ids restart with every database, so a previous test's
    # throttle entries would silence this one's first frame.
    comms.chat_service._typing_accepted.clear()


@pytest.fixture
def now(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ChatService, "clock", staticmethod(lambda: t[0]))
    return t


def typing_frame(server_id, channel_id):
    return {"type": "typing", "server_id": server_id, "channel_id": channel_id}


def assert_next_is_pong(ws):
    ws.send_json({"type": "ping", "t": 1.0})
    assert ws.receive_json() == {"type": "pong", "t": 1.0}


def test_channel_typing_reaches_other_member(two_members, now):
    alice_client, alice, bob_client, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        alice_ws.send_json(typing_frame(server["id"], channel["id"]))
        assert bob_ws.receive_json() == {
            "type": "typing", "user_id": alice["id"],
            "server_id": server["id"], "channel_id": channel["id"],
        }


def test_channel_typing_skips_all_of_the_senders_sockets(two_members, now):
    alice_client, _, bob_client, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            alice_client.websocket_connect("/ws", headers=HEADERS) as alice_tab, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(alice_tab)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online
        alice_tab.receive_json()

        alice_ws.send_json(typing_frame(server["id"], channel["id"]))
        assert bob_ws.receive_json()["type"] == "typing"

        # Had typing been echoed to her other tab, it would arrive before her
        # own chat message does.
        chat = chat_frame(server["id"], channel["id"])
        alice_ws.send_json(chat)
        assert bob_ws.receive_json()["id"] == chat["id"]
        assert alice_tab.receive_json()["id"] == chat["id"]
        assert_next_is_pong(alice_ws)


def test_channel_typing_does_not_reach_non_member(two_members, new_client, now):
    alice_client, _, bob_client, _, server, channel = two_members
    mallory_client = new_client()
    register(mallory_client, "mallory")
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            mallory_client.websocket_connect("/ws", headers=HEADERS) as mallory_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        ws_ready(mallory_ws)
        alice_ws.receive_json()  # bob online

        alice_ws.send_json(typing_frame(server["id"], channel["id"]))
        assert bob_ws.receive_json()["type"] == "typing"
        assert_next_is_pong(mallory_ws)


def test_typing_from_non_member_is_dropped_silently(two_members, new_client, now):
    alice_client, _, bob_client, _, server, channel = two_members
    mallory_client = new_client()
    register(mallory_client, "mallory")
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            mallory_client.websocket_connect("/ws", headers=HEADERS) as mallory_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        ws_ready(mallory_ws)
        alice_ws.receive_json()  # bob online

        mallory_ws.send_json(typing_frame(server["id"], channel["id"]))
        assert_next_is_pong(mallory_ws)

        chat = chat_frame(server["id"], channel["id"])
        alice_ws.send_json(chat)
        assert bob_ws.receive_json()["id"] == chat["id"]


def test_typing_in_channel_of_another_server_is_dropped_silently(two_members, now):
    alice_client, _, bob_client, _, server, channel = two_members
    other_server = create_server(alice_client, "Other")
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        alice_ws.send_json(typing_frame(other_server["id"], channel["id"]))
        assert_next_is_pong(alice_ws)

        chat = chat_frame(server["id"], channel["id"])
        alice_ws.send_json(chat)
        assert bob_ws.receive_json()["id"] == chat["id"]


def test_typing_is_throttled_per_sender_and_thread(two_members, now):
    alice_client, alice, bob_client, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        frame = typing_frame(server["id"], channel["id"])
        alice_ws.send_json(frame)
        assert bob_ws.receive_json()["type"] == "typing"

        now[0] += TYPING_MIN_INTERVAL / 2
        alice_ws.send_json(frame)
        chat = chat_frame(server["id"], channel["id"])
        alice_ws.send_json(chat)
        assert bob_ws.receive_json()["id"] == chat["id"]

        now[0] += TYPING_MIN_INTERVAL
        alice_ws.send_json(frame)
        assert bob_ws.receive_json() == {
            "type": "typing", "user_id": alice["id"],
            "server_id": server["id"], "channel_id": channel["id"],
        }


def test_dm_typing_reaches_only_the_peer(client, new_client, now):
    alice = register(client, "alice-typing")
    bob_client = new_client()
    bob = register(bob_client, "bob-typing")
    carol_client = new_client()
    register(carol_client, "carol-typing")
    convo = open_conversation(client, bob["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            client.websocket_connect("/ws", headers=HEADERS) as alice_tab, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        for ws in (alice_ws, alice_tab, bob_ws, carol_ws):
            ws_ready(ws)

        alice_ws.send_json({"type": "typing", "conversation_id": convo["id"]})
        assert bob_ws.receive_json() == {
            "type": "typing", "user_id": alice["id"], "conversation_id": convo["id"]}

        assert_next_is_pong(alice_ws)
        assert_next_is_pong(alice_tab)
        assert_next_is_pong(carol_ws)


def test_dm_typing_from_outsider_is_dropped_silently(client, new_client, now):
    register(client, "alice-typing")
    bob_client = new_client()
    bob = register(bob_client, "bob-typing")
    carol_client = new_client()
    register(carol_client, "carol-typing")
    convo = open_conversation(client, bob["id"])

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        ws_ready(bob_ws)
        ws_ready(carol_ws)

        carol_ws.send_json({"type": "typing", "conversation_id": convo["id"]})
        assert_next_is_pong(carol_ws)
        assert_next_is_pong(bob_ws)
