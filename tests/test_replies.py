"""Quote replies: a message can point at one earlier message in the same
channel or conversation, and every payload carries a small quote of it."""
import uuid
from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image

from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready

HEADERS = {"origin": ORIGIN}


def doc(text):
    return {"type": "doc", "content": [{"type": "paragraph",
                                        "content": [{"type": "text", "text": text}]}]}


def channel_frame(server_id, channel_id, text="hi", reply_to=None, attachment_ids=()):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": doc(text) if text is not None else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reply_to": reply_to,
        "attachment_ids": list(attachment_ids),
    }


def dm_frame(conversation_id, text="hi", reply_to=None):
    return {
        "type": "direct_message",
        "id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": doc(text),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reply_to": reply_to,
    }


def recv(ws, wanted_type):
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def attachments_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(tmp_path / "attachments"))


@pytest.fixture
def team(client, new_client):
    alice = register(client, "alice-rep")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    other_channel = create_channel(client, server["id"], name="other")
    bob_client = new_client()
    bob = register(bob_client, "bob-rep")
    invite = client.post("/invites/", json={"server_id": server["id"]})
    assert bob_client.post(f"/invites/{invite.json()['id']}/use", json={}).status_code == 200
    return {
        "alice": client, "alice_user": alice, "bob": bob_client, "bob_user": bob,
        "server": server, "channel": channel, "other_channel": other_channel,
    }


def history(client, channel_id):
    res = client.get(f"/channels/{channel_id}/messages")
    assert res.status_code == 200, res.text
    return {m["id"]: m for m in res.json()["messages"]}


def send_channel(alice_ws, bob_ws, team, **kwargs):
    frame = channel_frame(team["server"]["id"], team["channel"]["id"], **kwargs)
    alice_ws.send_json(frame)
    return recv(bob_ws, "message")


def test_channel_reply_carries_quote_live_and_in_history(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(alice_ws, bob_ws, team, text="the original")
        assert original["reply_to"] is None

        live = send_channel(alice_ws, bob_ws, team, text="answer", reply_to=original["id"])

    expected = {"id": original["id"], "user_id": team["alice_user"]["id"],
                "preview": "the original", "imported_author": None}
    assert live["reply_to"] == expected
    stored = history(team["bob"], team["channel"]["id"])
    assert stored[live["id"]]["reply_to"] == expected
    assert stored[original["id"]]["reply_to"] is None


def test_reply_to_a_reply_quotes_the_direct_parent(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        first = send_channel(alice_ws, bob_ws, team, text="first")
        second = send_channel(alice_ws, bob_ws, team, text="second", reply_to=first["id"])
        third = send_channel(alice_ws, bob_ws, team, text="third", reply_to=second["id"])

    assert third["reply_to"]["id"] == second["id"]
    assert third["reply_to"]["preview"] == "second"


def test_delta_sync_carries_reply_to(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(alice_ws, bob_ws, team, text="the original")
        reply = send_channel(alice_ws, bob_ws, team, text="answer", reply_to=original["id"])

    res = team["bob"].get("/channels/messages", params={"last_updated": "2000-01-01T00:00:00Z"})
    assert res.status_code == 200, res.text
    by_id = {m["id"]: m for m in res.json()}
    assert by_id[reply["id"]]["reply_to"]["preview"] == "the original"
    assert by_id[original["id"]]["reply_to"] is None


def test_reply_to_attachment_only_message_previews_the_attachment(team):
    upload = team["alice"].post(
        "/attachments",
        files={"file": ("pic.png", png_bytes(), "image/png")},
        data={"channel_id": str(team["channel"]["id"])},
    )
    assert upload.status_code in (200, 201), upload.text

    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(
            alice_ws, bob_ws, team, text=None, attachment_ids=[upload.json()["id"]])
        reply = send_channel(alice_ws, bob_ws, team, text="nice", reply_to=original["id"])

    assert reply["reply_to"]["preview"] == "Sent an image"
    stored = history(team["bob"], team["channel"]["id"])
    assert stored[reply["id"]]["reply_to"]["preview"] == "Sent an image"


def test_deleted_original_shows_as_deleted(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(alice_ws, bob_ws, team, text="oops")
        reply = send_channel(alice_ws, bob_ws, team, text="answer", reply_to=original["id"])

    assert team["alice"].delete(f"/messages/{original['id']}").status_code == 204
    stored = history(team["bob"], team["channel"]["id"])
    assert stored[reply["id"]]["reply_to"] == {"id": original["id"], "deleted": True}


def test_editing_a_reply_keeps_reply_to(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(alice_ws, bob_ws, team, text="the original")
        reply = send_channel(alice_ws, bob_ws, team, text="answer", reply_to=original["id"])

        res = team["alice"].patch(f"/messages/{reply['id']}", json={"content": doc("edited")})
        assert res.status_code == 200, res.text
        frame = recv(bob_ws, "message_updated")

    expected = {"id": original["id"], "user_id": team["alice_user"]["id"],
                "preview": "the original", "imported_author": None}
    assert res.json()["reply_to"] == expected
    assert frame["reply_to"] == expected


def test_editing_a_plain_message_has_null_reply_to(team):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        original = send_channel(alice_ws, bob_ws, team, text="plain")
        res = team["alice"].patch(f"/messages/{original['id']}", json={"content": doc("x")})

    assert res.json()["reply_to"] is None


def test_reply_to_message_in_another_channel_is_rejected(team):
    other_id = team["other_channel"]["id"]
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        elsewhere = channel_frame(team["server"]["id"], other_id, text="elsewhere")
        alice_ws.send_json(elsewhere)
        bad = channel_frame(
            team["server"]["id"], team["channel"]["id"], reply_to=elsewhere["id"])
        alice_ws.send_json(bad)
        error = recv(alice_ws, "error")

    assert error == {"type": "error", "code": "invalid_reply", "ref": bad["id"]}
    assert bad["id"] not in history(team["alice"], team["channel"]["id"])


@pytest.mark.parametrize("reply_to", [str(uuid.uuid4()), "not-a-uuid"])
def test_reply_to_unknown_message_is_rejected(team, reply_to):
    with team["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        bad = channel_frame(
            team["server"]["id"], team["channel"]["id"], reply_to=reply_to)
        alice_ws.send_json(bad)
        error = recv(alice_ws, "error")

    assert error == {"type": "error", "code": "invalid_reply", "ref": bad["id"]}
    assert history(team["alice"], team["channel"]["id"]) == {}


@pytest.fixture
def dms(client, new_client):
    alice = register(client, "alice-repdm")
    bob_client = new_client()
    bob = register(bob_client, "bob-repdm")
    carol_client = new_client()
    carol = register(carol_client, "carol-repdm")
    ab = client.post("/conversations/", json={"user_id": bob["id"]}).json()
    ac = client.post("/conversations/", json={"user_id": carol["id"]}).json()
    return {"alice": client, "alice_user": alice, "bob": bob_client, "ab": ab, "ac": ac}


def dm_history(client, conversation_id):
    res = client.get(f"/conversations/{conversation_id}/messages")
    assert res.status_code == 200, res.text
    return {m["id"]: m for m in res.json()["messages"]}


def test_dm_reply_carries_quote_live_and_in_history(dms):
    convo = dms["ab"]["id"]
    with dms["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            dms["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        original = dm_frame(convo, text="dm original")
        alice_ws.send_json(original)
        assert recv(bob_ws, "direct_message")["reply_to"] is None
        reply = dm_frame(convo, text="dm answer", reply_to=original["id"])
        alice_ws.send_json(reply)
        live = recv(bob_ws, "direct_message")

    expected = {"id": original["id"], "user_id": dms["alice_user"]["id"],
                "preview": "dm original", "imported_author": None}
    assert live["reply_to"] == expected
    stored = dm_history(dms["bob"], convo)
    assert stored[reply["id"]]["reply_to"] == expected
    assert stored[original["id"]]["reply_to"] is None


def test_dm_reply_to_message_in_another_conversation_is_rejected(dms):
    with dms["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws:
        elsewhere = dm_frame(dms["ac"]["id"], text="with carol")
        alice_ws.send_json(elsewhere)
        bad = dm_frame(dms["ab"]["id"], reply_to=elsewhere["id"])
        alice_ws.send_json(bad)
        error = recv(alice_ws, "error")

    assert error == {"type": "error", "code": "invalid_reply", "ref": bad["id"]}
    assert dm_history(dms["alice"], dms["ab"]["id"]) == {}


def test_dm_reply_to_unknown_message_is_rejected(dms):
    with dms["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws:
        for reply_to in (str(uuid.uuid4()), "nope"):
            bad = dm_frame(dms["ab"]["id"], reply_to=reply_to)
            alice_ws.send_json(bad)
            assert recv(alice_ws, "error")["code"] == "invalid_reply"
    assert dm_history(dms["alice"], dms["ab"]["id"]) == {}


def test_dm_deleted_original_shows_as_deleted(dms):
    convo = dms["ab"]["id"]
    with dms["alice"].websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            dms["bob"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        original = dm_frame(convo, text="oops")
        alice_ws.send_json(original)
        recv(bob_ws, "direct_message")
        reply = dm_frame(convo, reply_to=original["id"])
        alice_ws.send_json(reply)
        recv(bob_ws, "direct_message")

    assert dms["alice"].delete(f"/messages/{original['id']}").status_code == 204
    stored = dm_history(dms["bob"], convo)
    assert stored[reply["id"]]["reply_to"] == {"id": original["id"], "deleted": True}
