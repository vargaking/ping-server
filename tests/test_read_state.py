"""Per-channel and per-conversation read state: forward-only markers, list
responses carrying last_read/last_message/unread_count, the read_state
fan-out frame, and multi-socket support (several tabs per user)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.db_timing import own_query_stats
from app.models.Message import Message
from app.models.ReadState import ReadState
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.services.read_state import batch_channel_state, batch_channel_states
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}


def chat_frame(server_id, channel_id, **extra):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def dm_frame(conversation_id, **extra):
    return {
        "type": "direct_message",
        "id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def open_conversation(client, other_user_id):
    res = client.post("/conversations/", json={"user_id": other_user_id})
    assert res.status_code == 200, res.text
    return res.json()


def _recv(ws, wanted_type):
    """Drain frames until one of *wanted_type* arrives (presence noise first)."""
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


def _frames_until(ws, wanted_type):
    """Every frame up to and including the first *wanted_type* one."""
    frames = []
    for _ in range(10):
        frames.append(ws.receive_json())
        if frames[-1]["type"] == wanted_type:
            return frames
    raise AssertionError(f"no {wanted_type} frame arrived")


@pytest.fixture
def two_members(client, new_client):
    """alice (owner) and bob, both members of one server with one text channel."""
    alice = register(client, "alice-rs")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    bob = register(bob_client, "bob-rs")
    join(client, bob_client, server["id"])
    return client, alice, bob_client, bob, server, channel


def _post_channel_message(ws_sender, ws_receiver, server_id, channel_id):
    frame = chat_frame(server_id, channel_id)
    ws_sender.send_json(frame)
    assert _recv(ws_receiver, "message")["id"] == frame["id"]
    return frame["id"]


# --- marker only moves forward ---------------------------------------------

def test_marker_only_moves_forward(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    msg_ids = []
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        for _ in range(3):
            msg_ids.append(_post_channel_message(alice_ws, bob_ws, server["id"], channel["id"]))

    # bob reads the 3rd message.
    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg_ids[2]})
    assert res.status_code == 200, res.text
    assert res.json()["last_read_message_id"] == msg_ids[2]

    # bob then "reads" the 1st message — older than what's stored, must be a no-op.
    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg_ids[0]})
    assert res.status_code == 200, res.text
    assert res.json()["last_read_message_id"] == msg_ids[2]

    listing = bob_client.get(f"/channels/{server['id']}").json()
    ch = next(c for c in listing if c["id"] == channel["id"])
    assert ch["last_read_message_id"] == msg_ids[2]


# --- authorization -----------------------------------------------------------

def test_non_member_cannot_mark_channel_read(two_members, new_client):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg_id = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])

    mallory_client = new_client()
    register(mallory_client, "mallory-rs")
    res = mallory_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg_id})
    assert res.status_code == 403


def test_non_participant_cannot_mark_conversation_read(client, new_client):
    alice = register(client, "alice-dmrs")
    bob_client = new_client()
    bob = register(bob_client, "bob-dmrs")
    carol_client = new_client()
    register(carol_client, "carol-dmrs")

    convo = open_conversation(client, bob["id"])
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo["id"])
        alice_ws.send_json(frame)
        assert _recv(bob_ws, "direct_message")["id"] == frame["id"]

    res = carol_client.put(f"/conversations/{convo['id']}/read", json={"message_id": frame["id"]})
    assert res.status_code == 404


def test_unknown_channel_read_is_404(client):
    register(client, "solo-rs")
    res = client.put("/channels/999999/read", json={"message_id": str(uuid.uuid4())})
    assert res.status_code == 404


def test_unknown_conversation_read_is_404(client):
    register(client, "solo-rs2")
    res = client.put("/conversations/999999/read", json={"message_id": str(uuid.uuid4())})
    assert res.status_code == 404


def test_message_from_another_channel_is_404(two_members, new_client):
    alice_client, alice, bob_client, bob, server, channel = two_members
    other_channel = create_channel(alice_client, server["id"], name="other")

    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg_id = _post_channel_message(alice_ws, bob_ws, server["id"], other_channel["id"])

    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg_id})
    assert res.status_code == 404


def test_message_from_another_conversation_is_404(client, new_client):
    alice = register(client, "alice-xrs")
    bob_client = new_client()
    bob = register(bob_client, "bob-xrs")
    carol_client = new_client()
    carol = register(carol_client, "carol-xrs")

    convo_ab = open_conversation(client, bob["id"])
    convo_ac = open_conversation(client, carol["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo_ab["id"])
        alice_ws.send_json(frame)
        assert _recv(bob_ws, "direct_message")["id"] == frame["id"]

    # A message that lives in convo_ab, marked against convo_ac.
    res = client.put(f"/conversations/{convo_ac['id']}/read", json={"message_id": frame["id"]})
    assert res.status_code == 404


def test_bad_uuid_string_is_404(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": "not-a-uuid"})
    assert res.status_code == 404


# --- list responses ----------------------------------------------------------

def test_channel_list_has_null_markers_before_anything_happens(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    listing = bob_client.get(f"/channels/{server['id']}").json()
    ch = next(c for c in listing if c["id"] == channel["id"])
    assert ch["last_read_message_id"] is None
    assert ch["last_message_id"] is None


def test_channel_list_reflects_read_and_last_message(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg1 = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])
        msg2 = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])

    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg1})
    assert res.status_code == 200

    listing = bob_client.get(f"/channels/{server['id']}").json()
    ch = next(c for c in listing if c["id"] == channel["id"])
    assert ch["last_read_message_id"] == msg1
    assert ch["last_message_id"] == msg2


def test_conversation_list_unread_count_only_counts_other_person(client, new_client):
    alice = register(client, "alice-uc")
    bob_client = new_client()
    bob = register(bob_client, "bob-uc")
    convo = open_conversation(client, bob["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        # alice sends 2 messages bob hasn't read yet.
        for _ in range(2):
            frame = dm_frame(convo["id"])
            alice_ws.send_json(frame)
            assert _recv(bob_ws, "direct_message")["id"] == frame["id"]

    listing = bob_client.get("/conversations/").json()
    convo_row = next(c for c in listing if c["id"] == convo["id"])
    assert convo_row["unread_count"] == 2
    assert convo_row["last_message_id"] is not None
    assert convo_row["last_read_message_id"] is None

    # bob replying advances his own marker past alice's 2 messages, so they
    # stop counting as unread — his own message never leaves a thread unread.
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        frame = dm_frame(convo["id"])
        bob_ws.send_json(frame)
        assert _recv(alice_ws, "direct_message")["id"] == frame["id"]

    listing = bob_client.get("/conversations/").json()
    convo_row = next(c for c in listing if c["id"] == convo["id"])
    assert convo_row["unread_count"] == 0


def test_conversation_list_last_read_and_last_message_null_when_nothing_sent(client, new_client):
    alice = register(client, "alice-nl")
    bob_client = new_client()
    bob = register(bob_client, "bob-nl")
    convo = open_conversation(client, bob["id"])

    listing = client.get("/conversations/").json()
    convo_row = next(c for c in listing if c["id"] == convo["id"])
    assert convo_row["last_read_message_id"] is None
    assert convo_row["last_message_id"] is None
    assert convo_row["unread_count"] == 0


# --- sending advances the sender's own marker --------------------------------

def test_sending_channel_message_advances_own_marker(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg_id = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])

    listing = alice_client.get(f"/channels/{server['id']}").json()
    ch = next(c for c in listing if c["id"] == channel["id"])
    assert ch["last_read_message_id"] == msg_id == ch["last_message_id"]


def test_sending_dm_advances_own_marker(client, new_client):
    alice = register(client, "alice-sm")
    bob_client = new_client()
    bob = register(bob_client, "bob-sm")
    convo = open_conversation(client, bob["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo["id"])
        alice_ws.send_json(frame)
        assert _recv(bob_ws, "direct_message")["id"] == frame["id"]

    listing = client.get("/conversations/").json()
    convo_row = next(c for c in listing if c["id"] == convo["id"])
    assert convo_row["last_read_message_id"] == frame["id"] == convo_row["last_message_id"]
    assert convo_row["unread_count"] == 0


# --- deleted marker message falls back ---------------------------------------

def test_deleted_marker_message_falls_back_to_previous_surviving_message(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg1 = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])
        msg2 = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])

    res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg2})
    assert res.status_code == 200
    assert res.json()["last_read_message_id"] == msg2

    # alice (the author) deletes the marker message.
    assert alice_client.delete(f"/messages/{msg2}").status_code == 204

    listing = bob_client.get(f"/channels/{server['id']}").json()
    ch = next(c for c in listing if c["id"] == channel["id"])
    assert ch["last_read_message_id"] == msg1


# --- read_state fan-out --------------------------------------------------------

def test_read_update_notifies_only_the_users_other_socket(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        msg_id = _post_channel_message(alice_ws, bob_ws, server["id"], channel["id"])

        # bob opens a second tab.
        with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws2:
            ws_ready(bob_ws2)

            res = bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": msg_id})
            assert res.status_code == 200

            frame = _recv(bob_ws2, "read_state")
            assert frame == {
                "type": "read_state",
                "channel_id": channel["id"],
                "server_id": server["id"],
                "conversation_id": None,
                "last_read_message_id": msg_id,
            }

            # alice (a different user) must not receive bob's read_state frame:
            # everything she sees before bob's probe message is checked.
            probe = chat_frame(server["id"], channel["id"])
            bob_ws.send_json(probe)
            frames = _frames_until(alice_ws, "message")
            assert frames[-1]["id"] == probe["id"]
            assert all(f["type"] != "read_state" for f in frames)


# --- multi-socket delivery -----------------------------------------------------

def test_two_sockets_for_one_user_both_receive_a_message_from_someone_else(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws1, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws2:
        ws_ready(bob_ws2)

        frame = chat_frame(server["id"], channel["id"])
        alice_ws.send_json(frame)

        assert _recv(bob_ws1, "message")["id"] == frame["id"]
        assert _recv(bob_ws2, "message")["id"] == frame["id"]


def test_senders_other_socket_gets_own_message_but_not_the_sending_socket(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws1, \
            alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws2, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws2)

        frame = chat_frame(server["id"], channel["id"])
        alice_ws1.send_json(frame)

        # bob (someone else) gets it.
        assert _recv(bob_ws, "message")["id"] == frame["id"]
        # alice's other tab gets it too...
        assert _recv(alice_ws2, "message")["id"] == frame["id"]

        # ...but the sending socket itself gets no echo: the next thing it
        # sees is a reply from bob, not its own frame played back.
        reply = chat_frame(server["id"], channel["id"])
        bob_ws.send_json(reply)
        assert _recv(alice_ws1, "message")["id"] == reply["id"]


# --- presence with multiple sockets --------------------------------------------

def test_closing_one_of_two_sockets_does_not_send_offline(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        assert ws_ready(alice_ws) == {"type": "presence_init", "user_ids": []}

        bob_ws1 = bob_client.websocket_connect("/ws", headers=HEADERS)
        ws1 = bob_ws1.__enter__()
        ws_ready(ws1)
        assert alice_ws.receive_json() == {
            "type": "presence_update", "user_id": bob["id"], "online": True}

        bob_ws2 = bob_client.websocket_connect("/ws", headers=HEADERS)
        ws2 = bob_ws2.__enter__()
        ws_ready(ws2)

        # Closing the first of bob's two sockets must not flap presence: he
        # is still online on the second. Probe with a chat frame on ws2 and
        # check alice sees the message before any presence_update.
        bob_ws1.__exit__(None, None, None)

        frame = chat_frame(server["id"], channel["id"])
        ws2.send_json(frame)
        delivered = alice_ws.receive_json()
        assert delivered["type"] == "message"
        assert delivered["id"] == frame["id"]

        bob_ws2.__exit__(None, None, None)
        assert alice_ws.receive_json() == {
            "type": "presence_update", "user_id": bob["id"], "online": False}


def test_closing_the_last_socket_sends_offline(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
            ws_ready(bob_ws)
            assert alice_ws.receive_json() == {
                "type": "presence_update", "user_id": bob["id"], "online": True}

        assert alice_ws.receive_json() == {
            "type": "presence_update", "user_id": bob["id"], "online": False}


# --- many users at once ----------------------------------------------------

def test_batch_channel_states_matches_one_user_at_a_time(two_members):
    alice_client, alice, bob_client, bob, server, channel = two_members
    other = create_channel(alice_client, server["id"], name="other")
    sid = server["id"]

    async def scenario():
        users = [await User.create(username=f"reader{i}", password_hash="x") for i in range(3)]
        for user in users:
            await UserToServer.create(user=user, server_id=sid)
        messages = []
        for _ in range(3):
            messages.append(await Message.create(
                uuid=uuid.uuid4(), content="x", author_id=alice["id"], server_id=sid,
                channel_id=channel["id"], timestamp=datetime.now(timezone.utc)))
        deleted_id = messages[1].id
        await messages[1].delete()
        read, behind, deleted = users
        await ReadState.create(user=read, channel_id=channel["id"],
                               last_read_message_id=messages[2].id)
        await ReadState.create(user=behind, channel_id=channel["id"],
                               last_read_message_id=messages[0].id)
        await ReadState.create(user=deleted, channel_id=channel["id"],
                               last_read_message_id=deleted_id)
        wanted = {user.id: [channel["id"], other["id"]] for user in users}

        with own_query_stats() as stats:
            batched = await batch_channel_states(wanted)
        one_by_one = {user_id: await batch_channel_state(user_id, ids)
                      for user_id, ids in wanted.items()}
        return batched, one_by_one, stats.count, [str(m.uuid) for m in messages]

    batched, one_by_one, queries, uuids = alice_client.portal.call(scenario)
    assert batched == one_by_one
    states = list(batched.values())
    assert [s[channel["id"]]["last_read_message_id"] for s in states] == [uuids[2], uuids[0], uuids[0]]
    assert all(s[channel["id"]]["last_message_id"] == uuids[2] for s in states)
    assert all(s[other["id"]] == {"last_read_message_id": None, "last_message_id": None}
               for s in states)
    assert queries == 5


def test_batch_channel_states_queries_do_not_grow_with_deleted_markers(two_members):
    alice_client, alice, _, _, server, channel = two_members
    other = create_channel(alice_client, server["id"], name="other")
    sid = server["id"]

    async def scenario():
        channels = {}
        for channel_id, deleted in ((channel["id"], 2), (other["id"], 0)):
            messages = [await Message.create(
                uuid=uuid.uuid4(), content="x", author_id=alice["id"], server_id=sid,
                channel_id=channel_id, timestamp=datetime.now(timezone.utc)) for _ in range(4)]
            deleted_id = messages[deleted].id
            await messages[deleted].delete()
            read = str(messages[deleted - 1].uuid) if deleted else None
            channels[channel_id] = (read, str(messages[3].uuid), deleted_id)
        counts, states = {}, {}
        for members in (2, 20):
            users = [await User.create(username=f"gone{members}_{i}", password_hash="x")
                     for i in range(members)]
            for user in users:
                for channel_id, (_, _, deleted_id) in channels.items():
                    await ReadState.create(
                        user=user, channel_id=channel_id, last_read_message_id=deleted_id)
            with own_query_stats() as stats:
                states[members] = await batch_channel_states(
                    {user.id: list(channels) for user in users})
            counts[members] = stats.count
        return counts, states, channels

    counts, states, channels = alice_client.portal.call(scenario)
    assert counts[2] == counts[20]
    for members, by_user in states.items():
        assert len(by_user) == members
        for per_channel in by_user.values():
            for channel_id, (read, last, _) in channels.items():
                assert per_channel[channel_id] == {
                    "last_read_message_id": read, "last_message_id": last}
