"""The sender gets a message_ack once its message is stored, and resending an
id is safe: the same author is re-acked, anyone else is told duplicate_id."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.models.Message import Message
from app.services.chat_service import ChatService
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_attachments import upload

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}


@pytest.fixture(autouse=True)
def attachments_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(tmp_path / "attachments"))


def channel_frame(server_id, channel_id, message_id=None, **extra):
    return {
        "type": "message",
        "id": message_id or str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def dm_frame(conversation_id, message_id=None, **extra):
    return {
        "type": "direct_message",
        "id": message_id or str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def ack(message_id):
    return {"type": "message_ack", "id": message_id}


def expect_quiet(ws):
    """Nothing is queued for *ws*: a ping is answered before anything else."""
    ws.send_json({"type": "ping", "t": 1})
    assert ws.receive_json() == {"type": "pong", "t": 1}


def count_rows(client, message_id):
    async def count():
        return await Message.filter(uuid=message_id).count()
    return client.portal.call(count)


def stored_content(client, message_id):
    async def content():
        return (await Message.get(uuid=message_id)).content
    return json.loads(client.portal.call(content))


def invite_and_join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


@pytest.fixture
def team(client, new_client):
    """alice (owner) and bob in one channel, erin outside, plus alice-bob DM."""
    register(client, "alice-ack")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    bob = register(bob_client, "bob-ack")
    invite_and_join(client, bob_client, server["id"])
    erin_client = new_client()
    register(erin_client, "erin-ack")
    res = client.post("/conversations/", json={"user_id": bob["id"]})
    assert res.status_code == 200, res.text
    return {
        "alice": client, "bob": bob_client, "erin": erin_client,
        "server": server, "channel": channel, "conversation": res.json(),
    }


@pytest.fixture
def sockets(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online
        yield alice_ws, bob_ws


def test_channel_sender_gets_ack_and_members_do_not(team, sockets):
    alice_ws, bob_ws = sockets
    frame = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(frame)

    assert bob_ws.receive_json()["id"] == frame["id"]
    assert alice_ws.receive_json() == ack(frame["id"])
    expect_quiet(bob_ws)
    expect_quiet(alice_ws)


def test_dm_sender_gets_ack_and_peer_does_not(team, sockets):
    alice_ws, bob_ws = sockets
    frame = dm_frame(team["conversation"]["id"])
    alice_ws.send_json(frame)

    assert bob_ws.receive_json()["id"] == frame["id"]
    assert alice_ws.receive_json() == ack(frame["id"])
    expect_quiet(bob_ws)
    expect_quiet(alice_ws)


def test_ack_goes_only_to_the_sending_socket(team, sockets):
    alice_ws, _ = sockets
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as other_tab:
        ws_ready(other_tab)
        frame = channel_frame(team["server"]["id"], team["channel"]["id"])
        alice_ws.send_json(frame)

        assert other_tab.receive_json()["id"] == frame["id"]
        assert alice_ws.receive_json() == ack(frame["id"])
        expect_quiet(other_tab)


def test_channel_resend_is_reacked_and_stored_once(team, sockets):
    alice_ws, bob_ws = sockets
    frame = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    assert bob_ws.receive_json()["id"] == frame["id"]
    expect_quiet(bob_ws)
    assert count_rows(team["alice"], frame["id"]) == 1


def test_dm_resend_is_reacked_and_stored_once(team, sockets):
    alice_ws, bob_ws = sockets
    frame = dm_frame(team["conversation"]["id"])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    assert bob_ws.receive_json()["id"] == frame["id"]
    expect_quiet(bob_ws)
    assert count_rows(team["alice"], frame["id"]) == 1


def test_resend_with_attachments_is_reacked(team, sockets):
    alice_ws, bob_ws = sockets
    res = upload(team["alice"], channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    frame = channel_frame(
        team["server"]["id"], team["channel"]["id"], attachment_ids=[res.json()["id"]])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    assert bob_ws.receive_json()["attachments"][0]["id"] == res.json()["id"]
    expect_quiet(bob_ws)


def test_dm_resend_with_attachments_is_reacked(team, sockets):
    alice_ws, _ = sockets
    conversation_id = team["conversation"]["id"]
    res = upload(team["alice"], conversation_id=conversation_id)
    assert res.status_code == 201, res.text
    frame = dm_frame(conversation_id, attachment_ids=[res.json()["id"]])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])

    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])


def test_same_id_from_another_user_is_a_duplicate_id(team, sockets):
    alice_ws, bob_ws = sockets
    original = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(original)
    assert alice_ws.receive_json() == ack(original["id"])
    assert bob_ws.receive_json()["id"] == original["id"]

    bob_ws.send_json({**original, "content": {"type": "doc", "content": []}})
    assert bob_ws.receive_json() == {
        "type": "error", "code": "duplicate_id", "ref": original["id"]}
    expect_quiet(alice_ws)

    assert count_rows(team["alice"], original["id"]) == 1
    assert stored_content(team["alice"], original["id"]) == DOC


def test_same_id_in_another_thread_is_a_duplicate_id(team, sockets):
    alice_ws, _ = sockets
    original = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(original)
    assert alice_ws.receive_json() == ack(original["id"])

    alice_ws.send_json(dm_frame(team["conversation"]["id"], message_id=original["id"]))
    assert alice_ws.receive_json() == {
        "type": "error", "code": "duplicate_id", "ref": original["id"]}
    assert count_rows(team["alice"], original["id"]) == 1


def test_forbidden_gets_no_ack(team):
    with team["erin"].websocket_connect("/ws", headers=HEADERS) as erin_ws:
        ws_ready(erin_ws)
        frame = channel_frame(team["server"]["id"], team["channel"]["id"])
        erin_ws.send_json(frame)
        assert erin_ws.receive_json() == {
            "type": "error", "code": "forbidden", "ref": frame["id"]}
        expect_quiet(erin_ws)
    assert count_rows(team["alice"], frame["id"]) == 0


def test_forbidden_is_not_masked_by_a_stored_id(team, sockets):
    alice_ws, _ = sockets
    original = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(original)
    assert alice_ws.receive_json() == ack(original["id"])

    with team["erin"].websocket_connect("/ws", headers=HEADERS) as erin_ws:
        ws_ready(erin_ws)
        erin_ws.send_json(original)
        assert erin_ws.receive_json()["code"] == "forbidden"


def test_invalid_attachments_gets_no_ack(team, sockets):
    alice_ws, _ = sockets
    frame = channel_frame(
        team["server"]["id"], team["channel"]["id"], attachment_ids=[str(uuid.uuid4())])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == {
        "type": "error", "code": "invalid_attachments", "ref": frame["id"]}
    expect_quiet(alice_ws)


def lose_the_existence_check_once(monkeypatch):
    """Make the first duplicate check miss, as when two sends of one id race."""
    original = ChatService._answer_duplicate
    calls = []

    async def flaky(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return False
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ChatService, "_answer_duplicate", flaky)


def test_create_race_by_same_author_is_acked(team, sockets, monkeypatch):
    alice_ws, bob_ws = sockets
    frame = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])
    assert bob_ws.receive_json()["id"] == frame["id"]

    lose_the_existence_check_once(monkeypatch)
    alice_ws.send_json(frame)

    assert alice_ws.receive_json() == ack(frame["id"])
    expect_quiet(alice_ws)
    expect_quiet(bob_ws)
    assert count_rows(team["alice"], frame["id"]) == 1


def test_create_race_by_another_user_is_a_duplicate_id(team, sockets, monkeypatch):
    alice_ws, bob_ws = sockets
    frame = channel_frame(team["server"]["id"], team["channel"]["id"])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])
    assert bob_ws.receive_json()["id"] == frame["id"]

    lose_the_existence_check_once(monkeypatch)
    bob_ws.send_json(frame)

    assert bob_ws.receive_json() == {
        "type": "error", "code": "duplicate_id", "ref": frame["id"]}
    expect_quiet(bob_ws)
    assert count_rows(team["alice"], frame["id"]) == 1


def test_dm_create_race_is_acked(team, sockets, monkeypatch):
    alice_ws, bob_ws = sockets
    frame = dm_frame(team["conversation"]["id"])
    alice_ws.send_json(frame)
    assert alice_ws.receive_json() == ack(frame["id"])
    assert bob_ws.receive_json()["id"] == frame["id"]

    lose_the_existence_check_once(monkeypatch)
    alice_ws.send_json(frame)

    assert alice_ws.receive_json() == ack(frame["id"])
    expect_quiet(alice_ws)
    assert count_rows(team["alice"], frame["id"]) == 1
