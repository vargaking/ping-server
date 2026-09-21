"""Editing and deleting messages over REST, with realtime fan-out.

The author may edit their own message; the author or the server owner may delete
one. Every other online member of the server sees a message_updated /
message_deleted frame, while the actor is excluded (they have the REST reply).
"""
import uuid
from datetime import datetime, timezone

from tests.conftest import ORIGIN, create_channel, create_server, register

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}
EDITED = {"type": "doc", "content": [{"type": "paragraph",
                                      "content": [{"type": "text", "text": "edited"}]}]}


def chat_frame(server_id, channel_id, message_id):
    return {
        "type": "message",
        "id": message_id,
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def invite_and_join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def setup_server_with_two_members(client, new_client):
    """alice (owner) + bob, both members of a server with one channel."""
    register(client, "alice-med")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    register(bob_client, "bob-med")
    invite_and_join(client, bob_client, server["id"])
    return server, channel, bob_client


def post_message(sender_ws, receiver_ws, server_id, channel_id):
    """Post a message from sender and wait for receiver to see it, which
    guarantees it is persisted before the test moves on. Returns its id."""
    message_id = str(uuid.uuid4())
    sender_ws.send_json(chat_frame(server_id, channel_id, message_id))
    frame = receiver_ws.receive_json()
    assert frame["type"] == "message" and frame["id"] == message_id
    return message_id


def test_author_edits_and_members_receive_update(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()  # presence_init
        bob_ws.receive_json()    # presence_init
        alice_ws.receive_json()  # bob online

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = client.patch(f"/messages/{message_id}", json={"content": EDITED})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == message_id
        assert body["content"] == EDITED
        assert body["edited_at"] is not None

        frame = bob_ws.receive_json()
        assert frame["type"] == "message_updated"
        assert frame["id"] == message_id
        assert frame["content"] == EDITED
        assert frame["edited_at"] is not None

        # The editor is excluded: alice's next frame is bob's new message, not
        # the message_updated she triggered.
        bob_id = post_message(bob_ws, alice_ws, server["id"], channel["id"])
        assert bob_id  # alice received a plain message frame, nothing queued before it


def test_only_author_can_edit(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()
        bob_ws.receive_json()
        alice_ws.receive_json()

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = bob_client.patch(f"/messages/{message_id}", json={"content": EDITED})
        assert res.status_code == 403, res.text


def test_author_deletes_and_members_receive_delete(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()
        bob_ws.receive_json()
        alice_ws.receive_json()

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = client.delete(f"/messages/{message_id}")
        assert res.status_code == 204, res.text

        frame = bob_ws.receive_json()
        assert frame["type"] == "message_deleted"
        assert frame["id"] == message_id
        assert frame["channel_id"] == channel["id"]


def test_owner_can_delete_others_message(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()
        bob_ws.receive_json()
        alice_ws.receive_json()

        # bob posts, alice (the owner) deletes it.
        message_id = post_message(bob_ws, alice_ws, server["id"], channel["id"])

        res = client.delete(f"/messages/{message_id}")
        assert res.status_code == 204, res.text

        frame = bob_ws.receive_json()
        assert frame["type"] == "message_deleted"
        assert frame["id"] == message_id


def test_non_owner_cannot_delete_others_message(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()
        bob_ws.receive_json()
        alice_ws.receive_json()

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = bob_client.delete(f"/messages/{message_id}")
        assert res.status_code == 403, res.text


def test_non_member_cannot_edit_or_delete(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    carol_client = new_client()
    register(carol_client, "carol-med")

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.receive_json()
        bob_ws.receive_json()
        alice_ws.receive_json()

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        assert carol_client.patch(
            f"/messages/{message_id}", json={"content": EDITED}).status_code == 403
        assert carol_client.delete(f"/messages/{message_id}").status_code == 403


def test_edit_missing_message_returns_404(client):
    register(client, "alice-404")
    res = client.patch(f"/messages/{uuid.uuid4()}", json={"content": EDITED})
    assert res.status_code == 404, res.text
