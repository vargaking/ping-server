"""Emoji reactions: REST add/remove, payload shape, realtime frames and limits."""
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from app.models.Message import Message
from app.models.Reaction import Reaction
from app.models.Role import Role
from app.permissions import Permission
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_direct_messages import open_conversation
from tests.test_permissions import set_roles
from tests.test_realtime_events import chat_frame, invite_and_join

HEADERS = {"origin": ORIGIN}
THUMBS_UP = "\U0001F44D"
HEART = "❤️"


def run(client, func, *args):
    return client.portal.call(func, *args)


def react_path(message_id, emoji):
    return f"/messages/{message_id}/reactions/{quote(emoji, safe='')}"


def make_message(client, **fields) -> str:
    message_uuid = uuid.uuid4()

    async def create():
        await Message.create(
            uuid=message_uuid, content="hi", timestamp=datetime.now(timezone.utc),
            **fields)

    run(client, create)
    return str(message_uuid)


@pytest.fixture
def channel_team(client, new_client):
    alice = register(client, "alice-rx")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    bob = register(bob_client, "bob-rx")
    invite_and_join(client, bob_client, server["id"])
    message_id = make_message(
        client, author_id=alice["id"], server_id=server["id"], channel_id=channel["id"])
    return {
        "alice": alice, "bob": bob, "bob_client": bob_client,
        "server": server, "channel": channel, "message_id": message_id,
    }


@pytest.fixture
def dm(client, new_client):
    alice = register(client, "alice-rxdm")
    bob_client = new_client()
    bob = register(bob_client, "bob-rxdm")
    carol_client = new_client()
    register(carol_client, "carol-rxdm")
    conversation = open_conversation(client, bob["id"])
    message_id = make_message(
        client, author_id=alice["id"], conversation_id=conversation["id"])
    return {
        "alice": alice, "bob": bob, "bob_client": bob_client,
        "carol_client": carol_client, "conversation": conversation,
        "message_id": message_id,
    }


def history(client, channel_id):
    res = client.get(f"/channels/{channel_id}/messages")
    assert res.status_code == 200, res.text
    return res.json()["messages"]


def recv_type(ws, wanted):
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted:
            return frame
    raise AssertionError(f"no {wanted} frame arrived")


def test_add_and_remove_reaction(client, channel_team):
    mid, cid = channel_team["message_id"], channel_team["channel"]["id"]

    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
    assert history(client, cid)[0]["reactions"] == [
        {"emoji": THUMBS_UP, "user_ids": [channel_team["alice"]["id"]]}]

    assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
    assert history(client, cid)[0]["reactions"] == []


def test_percent_encoded_emoji_in_raw_path(client, channel_team):
    mid = channel_team["message_id"]
    assert client.put(f"/messages/{mid}/reactions/%F0%9F%91%8D").status_code == 204
    reactions = run(client, lambda: Reaction.all().values_list("emoji", flat=True))
    assert reactions == [THUMBS_UP]


def test_add_and_remove_are_idempotent(client, channel_team):
    mid, cid = channel_team["message_id"], channel_team["channel"]["id"]

    assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204

    assert len(history(client, cid)[0]["reactions"][0]["user_ids"]) == 1
    assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
    assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204


def test_remove_only_touches_the_callers_reaction(client, channel_team):
    mid, cid = channel_team["message_id"], channel_team["channel"]["id"]
    bob_client = channel_team["bob_client"]
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
    assert bob_client.put(react_path(mid, THUMBS_UP)).status_code == 204

    assert bob_client.delete(react_path(mid, THUMBS_UP)).status_code == 204

    assert history(client, cid)[0]["reactions"] == [
        {"emoji": THUMBS_UP, "user_ids": [channel_team["alice"]["id"]]}]


def test_reactions_are_grouped_by_first_reaction_in_reaction_order(client, channel_team):
    mid, cid = channel_team["message_id"], channel_team["channel"]["id"]
    bob_client = channel_team["bob_client"]
    alice_id, bob_id = channel_team["alice"]["id"], channel_team["bob"]["id"]

    assert bob_client.put(react_path(mid, HEART)).status_code == 204
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
    assert client.put(react_path(mid, HEART)).status_code == 204

    assert history(client, cid)[0]["reactions"] == [
        {"emoji": HEART, "user_ids": [bob_id, alice_id]},
        {"emoji": THUMBS_UP, "user_ids": [alice_id]},
    ]


def test_reactions_survive_an_edit(client, channel_team):
    mid = channel_team["message_id"]
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204

    res = client.patch(f"/messages/{mid}", json={"content": "edited"})

    assert res.status_code == 200, res.text
    assert res.json()["reactions"] == [
        {"emoji": THUMBS_UP, "user_ids": [channel_team["alice"]["id"]]}]


def test_direct_message_reactions_in_history_and_preview(client, dm):
    mid, conversation_id = dm["message_id"], dm["conversation"]["id"]
    assert dm["bob_client"].put(react_path(mid, THUMBS_UP)).status_code == 204
    expected = [{"emoji": THUMBS_UP, "user_ids": [dm["bob"]["id"]]}]

    page = client.get(f"/conversations/{conversation_id}/messages").json()
    assert page["messages"][0]["reactions"] == expected

    listing = client.get("/conversations/").json()
    assert listing[0]["last_message"]["reactions"] == expected


def test_direct_message_reactions_are_participants_only(client, dm):
    mid = dm["message_id"]
    carol = dm["carol_client"]

    assert carol.put(react_path(mid, THUMBS_UP)).status_code == 404
    assert carol.delete(react_path(mid, THUMBS_UP)).status_code == 404
    assert dm["bob_client"].put(react_path(mid, THUMBS_UP)).status_code == 204


def test_unknown_message_is_404(client, channel_team):
    assert client.put(react_path(str(uuid.uuid4()), THUMBS_UP)).status_code == 404
    assert client.put(react_path("not-a-uuid", THUMBS_UP)).status_code == 404


def test_non_member_cannot_react_in_a_channel(channel_team, new_client):
    outsider = new_client()
    register(outsider, "outsider-rx")
    res = outsider.put(react_path(channel_team["message_id"], THUMBS_UP))
    assert res.status_code == 403


def test_member_without_view_channel_cannot_react(client, channel_team):
    sid, bob_id = channel_team["server"]["id"], channel_team["bob"]["id"]

    async def make_role():
        role = await Role.create(
            server_id=sid, name="Blind", deny=int(Permission.VIEW_CHANNEL))
        return role.id

    role_id = run(client, make_role)
    assert set_roles(client, sid, bob_id, [role_id]).status_code == 200
    bob_client, mid = channel_team["bob_client"], channel_team["message_id"]

    assert bob_client.put(react_path(mid, THUMBS_UP)).status_code == 403
    assert bob_client.delete(react_path(mid, THUMBS_UP)).status_code == 403


@pytest.mark.parametrize("emoji", [
    "abc",
    THUMBS_UP + " ",
    " ",
    "a" + THUMBS_UP,
    "1",
    "́",
    THUMBS_UP * 33,
])
def test_invalid_emoji_is_400(client, channel_team, emoji):
    res = client.put(react_path(channel_team["message_id"], emoji))
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "Invalid emoji"


def test_empty_emoji_path_is_not_a_reaction(client, channel_team):
    res = client.put(f"/messages/{channel_team['message_id']}/reactions/")
    assert res.status_code in (404, 405)


@pytest.mark.parametrize("emoji", [
    THUMBS_UP,
    HEART,
    "\U0001F44D\U0001F3FD",
    "\U0001F468‍\U0001F469‍\U0001F467",
    "\U0001F1EA\U0001F1FA",
    "1️⃣",
    "#️⃣",
    "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
    "©",
    THUMBS_UP * 32,
])
def test_valid_emoji_is_accepted(client, channel_team, emoji):
    res = client.put(react_path(channel_team["message_id"], emoji))
    assert res.status_code == 204, res.text


def test_delete_rejects_invalid_emoji(client, channel_team):
    assert client.delete(react_path(channel_team["message_id"], "abc")).status_code == 400


def test_twenty_first_distinct_emoji_is_rejected(client, channel_team):
    mid, cid = channel_team["message_id"], channel_team["channel"]["id"]
    emojis = [chr(0x1F600 + i) for i in range(21)]

    for emoji in emojis[:20]:
        assert client.put(react_path(mid, emoji)).status_code == 204

    res = client.put(react_path(mid, emojis[20]))
    assert res.status_code == 400
    assert res.json()["detail"] == "Too many reactions"

    # An emoji already on the message is still allowed, from anyone.
    assert channel_team["bob_client"].put(react_path(mid, emojis[0])).status_code == 204
    assert len(history(client, cid)[0]["reactions"]) == 20


def test_frames_reach_members_including_the_actor(client, channel_team):
    bob_client, mid = channel_team["bob_client"], channel_team["message_id"]
    alice_id = channel_team["alice"]["id"]
    expected = {
        "message_id": mid,
        "server_id": channel_team["server"]["id"],
        "channel_id": channel_team["channel"]["id"],
        "conversation_id": None,
        "emoji": THUMBS_UP,
        "user_id": alice_id,
    }

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
        assert recv_type(alice_ws, "reaction_added") == {"type": "reaction_added", **expected}
        assert recv_type(bob_ws, "reaction_added") == {"type": "reaction_added", **expected}

        assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
        assert recv_type(alice_ws, "reaction_removed") == {"type": "reaction_removed", **expected}
        assert recv_type(bob_ws, "reaction_removed") == {"type": "reaction_removed", **expected}


def test_no_frame_when_nothing_changed(client, channel_team):
    mid = channel_team["message_id"]

    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
        assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
        assert recv_type(ws, "reaction_added")["emoji"] == THUMBS_UP
        assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
        assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
        # The next frame is the removal: the no-op delete and repeat put sent none.
        assert recv_type(ws, "reaction_removed")["emoji"] == THUMBS_UP


def test_frames_do_not_reach_outsiders(client, channel_team, new_client):
    outsider = new_client()
    register(outsider, "outsider-rxws")
    mid = channel_team["message_id"]

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            outsider.websocket_connect("/ws", headers=HEADERS) as outsider_ws:
        ws_ready(alice_ws)
        ws_ready(outsider_ws)

        assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
        recv_type(alice_ws, "reaction_added")
        assert client.delete(react_path(mid, THUMBS_UP)).status_code == 204
        recv_type(alice_ws, "reaction_removed")

        # A forbidden reply is the first thing the outsider's socket yields,
        # so no reaction frame was queued ahead of it.
        outsider_ws.send_json(chat_frame(
            channel_team["server"]["id"], channel_team["channel"]["id"]))
        assert outsider_ws.receive_json()["type"] == "error"


def test_direct_message_frames_reach_both_participants_only(client, dm):
    mid = dm["message_id"]
    carol = dm["carol_client"]

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            dm["bob_client"].websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)

        assert dm["bob_client"].put(react_path(mid, THUMBS_UP)).status_code == 204

        for ws in (alice_ws, bob_ws):
            frame = recv_type(ws, "reaction_added")
            assert frame["conversation_id"] == dm["conversation"]["id"]
            assert frame["server_id"] is None and frame["channel_id"] is None
            assert frame["user_id"] == dm["bob"]["id"]

    assert carol.put(react_path(mid, THUMBS_UP)).status_code == 404


def test_deleting_a_message_removes_its_reactions(client, channel_team):
    sid, cid = channel_team["server"]["id"], channel_team["channel"]["id"]
    mid = channel_team["message_id"]
    other_id = make_message(
        client, author_id=channel_team["alice"]["id"], server_id=sid, channel_id=cid)
    assert client.put(react_path(mid, THUMBS_UP)).status_code == 204
    assert channel_team["bob_client"].put(react_path(mid, HEART)).status_code == 204
    assert client.put(react_path(other_id, HEART)).status_code == 204

    assert client.delete(f"/messages/{mid}").status_code == 204

    remaining = run(client, lambda: Reaction.all().count())
    assert remaining == 1
    messages = history(client, cid)
    assert [m["id"] for m in messages] == [other_id]
    assert messages[0]["reactions"] == [
        {"emoji": HEART, "user_ids": [channel_team["alice"]["id"]]}]
