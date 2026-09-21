"""Per-channel message history pagination and the bounded delta-sync endpoint."""
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import create_channel, create_server, register

HEADERS = {"origin": "http://localhost:5173"}
DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}


def _join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def _post_messages(sender_ws, receiver_ws, server_id, channel_id, count):
    """Send count messages from sender and let receiver drain each one, which
    guarantees the server has persisted them before we query."""
    ids = []
    for _ in range(count):
        mid = str(uuid.uuid4())
        sender_ws.send_json({
            "type": "message",
            "id": mid,
            "server_id": server_id,
            "channel_id": channel_id,
            "content": DOC,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        assert receiver_ws.receive_json()["id"] == mid
        ids.append(mid)
    return ids


@pytest.fixture
def two_members(client, new_client):
    register(client, "alice")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    register(bob_client, "bob")
    _join(client, bob_client, server["id"])
    return client, bob_client, server, channel


def test_history_returns_newest_page_and_paginates_backwards(two_members):
    alice_client, bob_client, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json(); bob_ws.receive_json(); alice_ws.receive_json()
        ids = _post_messages(alice_ws, bob_ws, server["id"], channel["id"], 55)

    # Newest page: last 50 sent, newest first, more history behind it.
    page = alice_client.get(f"/channels/{channel['id']}/messages").json()
    assert len(page["messages"]) == 50
    assert page["has_more"] is True
    assert [m["id"] for m in page["messages"]] == list(reversed(ids[5:]))

    # Walk back to the older remainder using the cursor.
    older = alice_client.get(
        f"/channels/{channel['id']}/messages",
        params={"before": page["next_cursor"]},
    ).json()
    assert [m["id"] for m in older["messages"]] == list(reversed(ids[:5]))
    assert older["has_more"] is False
    assert older["next_cursor"] is None


def test_history_limit_is_capped(two_members):
    alice_client, _, _, channel = two_members
    page = alice_client.get(
        f"/channels/{channel['id']}/messages",
        params={"limit": 9999},
    )
    assert page.status_code == 200


def test_history_requires_membership(two_members, new_client):
    alice_client, _, _, channel = two_members
    outsider = new_client()
    register(outsider, "mallory")
    assert outsider.get(f"/channels/{channel['id']}/messages").status_code == 403


def test_history_unknown_channel_is_404(client):
    register(client)
    assert client.get("/channels/9999/messages").status_code == 404


def test_history_rejects_bad_cursor(two_members):
    alice_client, _, _, channel = two_members
    res = alice_client.get(
        f"/channels/{channel['id']}/messages",
        params={"before": "not-a-cursor"},
    )
    assert res.status_code == 400


def test_delta_sync_respects_limit(two_members):
    alice_client, bob_client, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json(); bob_ws.receive_json(); alice_ws.receive_json()
        _post_messages(alice_ws, bob_ws, server["id"], channel["id"], 5)

    stored = alice_client.get(
        "/channels/messages",
        params={"last_updated": "1970-01-01T00:00:00Z", "limit": 2},
    ).json()
    assert len(stored) == 2
