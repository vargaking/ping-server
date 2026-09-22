"""ZET-55: 1:1 direct messages.

Conversations are their own entity (not a hidden 2-member server) and any user
can DM any other user — no shared-server restriction. These tests drive the
public REST + WebSocket API, so they double as a contract for the frontend.
"""
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import ORIGIN, register

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}


def dm_frame(conversation_id, **extra):
    return {
        "type": "direct_message",
        "id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def open_conversation(client, other_user_id):
    res = client.post("/conversations/", json={"user_id": other_user_id})
    assert res.status_code == 200, res.text
    return res.json()


def _recv(ws, wanted_type):
    """Drain frames until one of *wanted_type* arrives.

    Connecting sockets may receive presence frames first; those are noise for
    the DM assertions here.
    """
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


@pytest.fixture
def three_users(client, new_client):
    """alice, bob, carol — three independent accounts, no servers involved."""
    alice = register(client, "alice-dm")
    bob_client = new_client()
    bob = register(bob_client, "bob-dm")
    carol_client = new_client()
    carol = register(carol_client, "carol-dm")
    return (client, alice), (bob_client, bob), (carol_client, carol)


# --- get-or-create ---------------------------------------------------------

def test_open_conversation_returns_other_user(three_users):
    (alice_client, _), (_, bob), _ = three_users
    convo = open_conversation(alice_client, bob["id"])
    assert convo["other_user"]["id"] == bob["id"]
    assert convo["other_user"]["username"] == bob["username"]
    assert convo["last_message"] is None


def test_get_or_create_is_idempotent(three_users):
    (alice_client, _), (_, bob), _ = three_users
    first = open_conversation(alice_client, bob["id"])
    second = open_conversation(alice_client, bob["id"])
    assert first["id"] == second["id"]


def test_get_or_create_is_order_independent(three_users):
    """A opening B and B opening A resolve to the same conversation."""
    (alice_client, alice), (bob_client, bob), _ = three_users
    from_alice = open_conversation(alice_client, bob["id"])
    from_bob = open_conversation(bob_client, alice["id"])
    assert from_alice["id"] == from_bob["id"]
    # Each side sees the *other* person as other_user.
    assert from_alice["other_user"]["id"] == bob["id"]
    assert from_bob["other_user"]["id"] == alice["id"]


def test_cannot_open_conversation_with_yourself(three_users):
    (alice_client, alice), _, _ = three_users
    res = alice_client.post("/conversations/", json={"user_id": alice["id"]})
    assert res.status_code == 400


def test_open_conversation_with_unknown_user_is_404(three_users):
    (alice_client, _), _, _ = three_users
    res = alice_client.post("/conversations/", json={"user_id": 999999})
    assert res.status_code == 404


# --- listing ---------------------------------------------------------------

def test_list_conversations_includes_last_message_and_sorts_by_activity(three_users):
    (alice_client, _), (bob_client, bob), (carol_client, carol) = three_users
    convo_bob = open_conversation(alice_client, bob["id"])
    convo_carol = open_conversation(alice_client, carol["id"])

    # Send one message in the bob conversation only; it should sort to the top
    # and carry a preview.
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo_bob["id"])
        alice_ws.send_json(frame)
        assert _recv(bob_ws, "direct_message")["id"] == frame["id"]

    listing = alice_client.get("/conversations/").json()
    assert [c["id"] for c in listing] == [convo_bob["id"], convo_carol["id"]]
    top = listing[0]
    assert top["last_message"]["id"] == frame["id"]
    assert top["last_message"]["user_id"] == alice_client.get("/auth/me").json()["id"]
    assert listing[1]["last_message"] is None


def test_list_only_shows_your_conversations(three_users):
    (alice_client, alice), (bob_client, bob), (carol_client, carol) = three_users
    open_conversation(alice_client, bob["id"])
    # Carol has no conversations.
    assert carol_client.get("/conversations/").json() == []


# --- websocket delivery & authorization ------------------------------------

def test_dm_is_delivered_to_peer_live(three_users):
    (alice_client, _), (bob_client, bob), _ = three_users
    convo = open_conversation(alice_client, bob["id"])

    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo["id"])
        alice_ws.send_json(frame)
        received = _recv(bob_ws, "direct_message")

    assert received["id"] == frame["id"]
    assert received["conversation_id"] == convo["id"]
    assert received["content"] == DOC
    # Identity comes from the socket, echoed back as user_id.
    alice_id = alice_client.get("/auth/me").json()["id"]
    assert received["user_id"] == alice_id


def test_dm_frame_ignores_client_supplied_user_id(three_users):
    """A forged user_id in the frame must not reach the peer."""
    (alice_client, alice), (bob_client, bob), _ = three_users
    convo = open_conversation(alice_client, bob["id"])

    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.send_json(dm_frame(convo["id"], user_id=99999))
        received = _recv(bob_ws, "direct_message")

    assert received["user_id"] == alice["id"]


def test_non_participant_cannot_post_over_ws(three_users):
    (alice_client, _), (_, bob), (carol_client, _) = three_users
    convo = open_conversation(alice_client, bob["id"])

    with carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        frame = dm_frame(convo["id"])
        carol_ws.send_json(frame)
        # Carol gets a forbidden error referencing her frame, not silent success.
        err = _recv(carol_ws, "error")
        assert err["code"] == "forbidden"
        assert err["ref"] == frame["id"]

    # And nothing was persisted to the conversation.
    assert alice_client.get(f"/conversations/{convo['id']}/messages").json()["messages"] == []


# --- history ---------------------------------------------------------------

def test_history_returns_persisted_messages(three_users):
    (alice_client, _), (bob_client, bob), _ = three_users
    convo = open_conversation(alice_client, bob["id"])

    sent = []
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        for _ in range(3):
            frame = dm_frame(convo["id"])
            alice_ws.send_json(frame)
            assert _recv(bob_ws, "direct_message")["id"] == frame["id"]
            sent.append(frame["id"])

    page = alice_client.get(f"/conversations/{convo['id']}/messages").json()
    assert [m["id"] for m in page["messages"]] == list(reversed(sent))
    assert page["has_more"] is False
    assert all(m["conversation_id"] == convo["id"] for m in page["messages"])


def test_history_paginates_backwards(three_users):
    (alice_client, _), (bob_client, bob), _ = three_users
    convo = open_conversation(alice_client, bob["id"])

    sent = []
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        for _ in range(55):
            frame = dm_frame(convo["id"])
            alice_ws.send_json(frame)
            assert _recv(bob_ws, "direct_message")["id"] == frame["id"]
            sent.append(frame["id"])

    page = alice_client.get(f"/conversations/{convo['id']}/messages").json()
    assert len(page["messages"]) == 50
    assert page["has_more"] is True

    older = alice_client.get(
        f"/conversations/{convo['id']}/messages",
        params={"before": page["next_cursor"]},
    ).json()
    assert [m["id"] for m in older["messages"]] == list(reversed(sent[:5]))
    assert older["has_more"] is False


def test_non_participant_cannot_read_history(three_users):
    (alice_client, _), (_, bob), (carol_client, _) = three_users
    convo = open_conversation(alice_client, bob["id"])
    # 404, not 403: don't confirm the conversation exists to an outsider.
    assert carol_client.get(f"/conversations/{convo['id']}/messages").status_code == 404


def test_history_unknown_conversation_is_404(three_users):
    (alice_client, _), _, _ = three_users
    assert alice_client.get("/conversations/999999/messages").status_code == 404
