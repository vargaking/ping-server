"""Editing and deleting messages over REST, with realtime fan-out.

The author may edit their own message; the author or the server owner may delete
one. Every online member of the server sees a message_updated /
message_deleted frame, the actor's own sockets included, so their other tabs
and devices stay in step.
"""
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from app.permissions import Permission
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_channel_permissions import overwrite, post_message as post_as
from tests.test_calls import (  # noqa: F401
    calls, clock, livekit_env, pair, rooms, started, stored, stored_message, text_of)
from tests.test_permissions import promote

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


def next_of_type(ws, frame_type):
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == frame_type:
            return frame
    raise AssertionError(f"no {frame_type} frame arrived")


def post_message(sender_ws, receiver_ws, server_id, channel_id):
    """Post a message from sender and wait for receiver to see it, which
    guarantees it is persisted before the test moves on. Returns its id."""
    message_id = str(uuid.uuid4())
    sender_ws.send_json(chat_frame(server_id, channel_id, message_id))
    frame = receiver_ws.receive_json()
    assert frame["type"] == "message" and frame["id"] == message_id
    assert sender_ws.receive_json() == {"type": "message_ack", "id": message_id}
    return message_id


def test_author_edits_and_members_receive_update(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
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

        own = alice_ws.receive_json()
        assert own["type"] == "message_updated"
        assert own["id"] == message_id
        assert own["content"] == EDITED


@pytest.mark.parametrize("act, frame_type", [
    pytest.param(lambda c, mid: c.patch(f"/messages/{mid}", json={"content": EDITED}),
                 "message_updated", id="edit"),
    pytest.param(lambda c, mid: c.delete(f"/messages/{mid}"), "message_deleted", id="delete"),
])
def test_the_actors_other_socket_receives_the_frame(client, new_client, act, frame_type):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            client.websocket_connect("/ws", headers=HEADERS) as alice_other_ws:
        ws_ready(alice_ws)
        ws_ready(alice_other_ws)

        message_id = str(uuid.uuid4())
        alice_ws.send_json(chat_frame(server["id"], channel["id"], message_id))
        assert next_of_type(alice_other_ws, "message")["id"] == message_id

        assert act(client, message_id).status_code in (200, 204)

        for ws in (alice_other_ws, alice_ws):
            assert next_of_type(ws, frame_type)["id"] == message_id


def test_only_author_can_edit(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = bob_client.patch(f"/messages/{message_id}", json={"content": EDITED})
        assert res.status_code == 403, res.text


def test_author_deletes_and_members_receive_delete(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = client.delete(f"/messages/{message_id}")
        assert res.status_code == 204, res.text

        frame = bob_ws.receive_json()
        assert frame["type"] == "message_deleted"
        assert frame["id"] == message_id
        assert frame["channel_id"] == channel["id"]

        own = alice_ws.receive_json()
        assert own["type"] == "message_deleted"
        assert own["id"] == message_id


def test_owner_can_delete_others_message(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

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
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = bob_client.delete(f"/messages/{message_id}")
        assert res.status_code == 403, res.text


@pytest.mark.parametrize("moderator, rule, in_that_channel, elsewhere", [
    pytest.param(False, {"allow": int(Permission.MANAGE_MESSAGES)}, 204, 403, id="granted by the rule"),
    pytest.param(True, {"deny": int(Permission.MANAGE_MESSAGES)}, 403, 204, id="taken by the rule"),
])
def test_a_channel_rule_grants_or_takes_manage_messages(
        client, new_client, moderator, rule, in_that_channel, elsewhere):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    carol_client = new_client()
    carol = register(carol_client, "carol-med")
    invite_and_join(client, carol_client, server["id"])
    other = create_channel(client, server["id"], "other")
    if moderator:
        promote(client, server["id"], carol["id"])
    path = f"/channels/{channel['id']}/permissions/members/{carol['id']}"
    assert overwrite(client, path, **rule).status_code == 200
    here = post_as(bob_client, server["id"], channel["id"])
    there = post_as(bob_client, server["id"], other["id"])

    assert carol_client.delete(f"/messages/{here}").status_code == in_that_channel
    assert carol_client.delete(f"/messages/{there}").status_code == elsewhere


def test_non_member_cannot_edit_or_delete(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    carol_client = new_client()
    register(carol_client, "carol-med")

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        assert carol_client.patch(
            f"/messages/{message_id}", json={"content": EDITED}).status_code == 403
        assert carol_client.delete(f"/messages/{message_id}").status_code == 403


def test_edit_missing_message_returns_404(client):
    register(client, "alice-404")
    res = client.patch(f"/messages/{uuid.uuid4()}", json={"content": EDITED})
    assert res.status_code == 404, res.text


THUMBS_UP = quote("👍", safe="")


@pytest.mark.parametrize("who", ["caller", "callee"])
@pytest.mark.parametrize("act, expected", [
    (lambda c, url: c.patch(url, json={"content": "x"}), 403),
    (lambda c, url: c.delete(url), 403),
    (lambda c, url: c.put(f"{url}/reactions/{THUMBS_UP}"), 400),
    (lambda c, url: c.delete(f"{url}/reactions/{THUMBS_UP}"), 400),
], ids=["edit", "delete", "react", "unreact"])
def test_call_rows_cannot_be_changed(calls, pair, who, act, expected):  # noqa: F811
    call = started(pair.a_client, pair.convo)

    res = act(pair.client_of[who], f"/messages/{call['message_id']}")

    assert res.status_code == expected
    assert stored(pair.a_client, call["id"]).state == "ringing"
    assert text_of(stored_message(pair.a_client, call).content) == "Started a call"


def test_outsiders_still_get_404_on_a_call_row(calls, pair, new_client):  # noqa: F811
    call = started(pair.a_client, pair.convo)
    carol = new_client()
    register(carol, "carol-row")
    url = f"/messages/{call['message_id']}"

    assert carol.patch(url, json={"content": "x"}).status_code == 404
    assert carol.delete(url).status_code == 404
    assert carol.put(f"{url}/reactions/{THUMBS_UP}").status_code == 404
