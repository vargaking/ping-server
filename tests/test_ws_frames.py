"""Frames that cannot be decoded get an error reply and never kill the socket."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.ws_schemas import MAX_FRAME_BYTES
from tests.test_websocket import HEADERS, chat_frame, two_members  # noqa: F401
from tests.conftest import ws_ready

INVALID_FRAME = {"type": "error", "code": "invalid_frame", "ref": None}


def open_sockets(two_members):
    alice_client, _, bob_client, _, server, channel = two_members
    alice_ws = alice_client.websocket_connect("/ws", headers=HEADERS)
    bob_ws = bob_client.websocket_connect("/ws", headers=HEADERS)
    return alice_ws, bob_ws, server, channel


def assert_chat_still_works(alice_ws, bob_ws, server, channel):
    frame = chat_frame(server["id"], channel["id"])
    alice_ws.send_json(frame)
    assert bob_ws.receive_json()["id"] == frame["id"]


def test_deeply_nested_frame_is_rejected_and_socket_keeps_working(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()  # bob online

        alice.send_text("[" * 1000 + "]" * 1000)
        assert alice.receive_json() == INVALID_FRAME

        assert_chat_still_works(alice, bob, server, channel)


def test_non_json_text_is_rejected_and_socket_stays_open(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        for garbage in ("not json", "", '{"type": "message"'):
            alice.send_text(garbage)
            assert alice.receive_json() == INVALID_FRAME

        assert_chat_still_works(alice, bob, server, channel)


@pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", "null", '"message"', "true"])
def test_non_object_json_is_rejected_and_socket_stays_open(two_members, payload):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        alice.send_text(payload)
        assert alice.receive_json() == INVALID_FRAME

        assert_chat_still_works(alice, bob, server, channel)


def test_binary_frame_is_rejected_and_socket_stays_open(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        alice.send_bytes(json.dumps({"type": "connection_init"}).encode())
        assert alice.receive_json() == INVALID_FRAME

        assert_chat_still_works(alice, bob, server, channel)


def frame_of_size(server_id, channel_id, size):
    """A valid chat frame whose serialized form is exactly *size* bytes."""
    frame = {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": ""}]}]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    overhead = len(json.dumps(frame).encode())
    frame["content"]["content"][0]["content"][0]["text"] = "x" * (size - overhead)
    text = json.dumps(frame)
    assert len(text.encode()) == size
    return frame, text


def test_frame_just_over_the_cap_is_rejected_and_socket_stays_open(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        _, text = frame_of_size(server["id"], channel["id"], MAX_FRAME_BYTES + 1)
        alice.send_text(text)
        assert alice.receive_json() == INVALID_FRAME

        assert_chat_still_works(alice, bob, server, channel)


def test_frame_at_the_cap_is_delivered(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        frame, text = frame_of_size(server["id"], channel["id"], MAX_FRAME_BYTES)
        alice.send_text(text)
        assert bob.receive_json()["id"] == frame["id"]


def test_each_bad_frame_logs_one_warning(two_members):
    alice_ws, bob_ws, server, channel = open_sockets(two_members)
    with alice_ws as alice, bob_ws as bob:
        ws_ready(alice)
        ws_ready(bob)
        alice.receive_json()

        with patch("app.communication.logger") as log:
            alice.send_text("[" * 1000 + "]" * 1000)
            alice.receive_json()
            alice.send_text("not json")
            alice.receive_json()

    assert log.warning.call_count == 2
    assert log.exception.call_count == 0
    assert all("exc_info" not in call.kwargs for call in log.warning.call_args_list)
