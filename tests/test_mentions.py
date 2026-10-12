"""Mention counts: which sends store a mention, how edits and deletes keep the
rows right, and the per-member count the channel list and snapshot carry."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db_timing import own_query_stats
from app.models.Channel import Channel
from app.models.Message import Message
from app.models.MessageMention import MessageMention
from app.models.User import User
from app.services.read_state import batch_mention_counts
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_channel_permissions import VIEW, overwrite
from tests.test_overwrite_fanout import channel_rule, listening, of_type
from tests.test_permissions import roles_by_name, run
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}


def doc(*nodes):
    return {"type": "doc", "content": [{"type": "paragraph", "content": list(nodes)}]}


def mention(user_id):
    return {"type": "mention", "attrs": {"id": user_id, "label": "x"}}


def text(value="hi"):
    return {"type": "text", "text": value}


def frame(server_id, channel_id, content, **extra):
    return {
        "type": "message", "id": str(uuid.uuid4()), "server_id": server_id,
        "channel_id": channel_id, "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(), **extra,
    }


def sync(ws):
    ws.send_json({"type": "connection_init"})
    while ws.receive_json()["type"] != "presence_init":
        pass


def send(client, outgoing):
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(outgoing)
        sync(ws)
    return outgoing["id"]


def post(client, channel_id, content):
    opening = {
        "id": str(uuid.uuid4()), "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    res = client.post(f"/channels/{channel_id}/posts", json={"title": "Post", "message": opening})
    assert res.status_code == 201, res.text
    return res.json()["post"]["id"], opening["id"]


def mentioned(client, message_uuid):
    async def rows():
        return sorted(await MessageMention.filter(
            message__uuid=uuid.UUID(message_uuid)).values_list("user_id", flat=True))

    return run(client, rows)


def stored_mentions(client):
    return run(client, MessageMention.all().count)


def counts(client, sid):
    res = client.get(f"/servers/{sid}/channels")
    assert res.status_code == 200, res.text
    return {c["id"]: c["mention_count"] for c in res.json()["channels"]}


@pytest.fixture
def team(client, new_client):
    alice = register(client, "alice-mentions")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    members = {}
    for name in ("bob", "carol"):
        member_client = new_client()
        user = register(member_client, f"{name}-mentions")
        invite_and_join(client, member_client, server["id"])
        members[name] = SimpleNamespace(client=member_client, user=user, id=user["id"])
    return SimpleNamespace(
        alice_client=client, alice=alice, sid=server["id"], channel=channel["id"], **members)


def hide_channel(team):
    everyone = roles_by_name(team.alice_client, team.sid)["@everyone"]
    path = channel_rule(team.channel, "roles", everyone["id"])
    assert overwrite(team.alice_client, path, deny=VIEW).status_code == 200
    return path


def test_a_mention_counts_for_the_mentioned_member(team):
    send(team.alice_client, frame(team.sid, team.channel, doc(mention(team.bob.id))))

    assert counts(team.bob.client, team.sid) == {team.channel: 1}
    assert counts(team.alice_client, team.sid) == {team.channel: 0}
    assert counts(team.carol.client, team.sid) == {team.channel: 0}


@pytest.mark.parametrize("target", ["sender", "non_viewer", "unknown_user"])
def test_mentions_that_store_nothing(team, target):
    if target != "non_viewer":
        send_to = {"sender": team.alice["id"], "unknown_user": 99999}[target]
    else:
        hide_channel(team)
        send_to = team.bob.id

    message = send(team.alice_client, frame(team.sid, team.channel, doc(mention(send_to))))

    assert run(team.alice_client, Message.filter(uuid=uuid.UUID(message)).count) == 1
    assert stored_mentions(team.alice_client) == 0


def test_a_dm_mention_stores_nothing(team):
    res = team.alice_client.post("/conversations/", json={"user_id": team.bob.id})
    assert res.status_code == 200, res.text
    outgoing = {
        "type": "direct_message", "id": str(uuid.uuid4()),
        "conversation_id": res.json()["id"], "content": doc(mention(team.bob.id)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    message = send(team.alice_client, outgoing)

    assert run(team.alice_client, Message.filter(uuid=uuid.UUID(message)).count) == 1
    assert stored_mentions(team.alice_client) == 0


def test_an_edit_adds_and_removes_mentions(team):
    message = send(team.alice_client, frame(
        team.sid, team.channel, doc(mention(team.bob.id), mention(team.alice["id"]))))
    assert mentioned(team.alice_client, message) == [team.bob.id]

    edited = team.alice_client.patch(
        f"/messages/{message}", json={"content": doc(mention(team.carol.id), mention(team.bob.id))})
    assert edited.status_code == 200, edited.text
    assert mentioned(team.alice_client, message) == sorted([team.bob.id, team.carol.id])

    edited = team.alice_client.patch(
        f"/messages/{message}", json={"content": doc(mention(team.carol.id))})
    assert edited.status_code == 200, edited.text
    assert mentioned(team.alice_client, message) == [team.carol.id]
    assert counts(team.bob.client, team.sid) == {team.channel: 0}
    assert counts(team.carol.client, team.sid) == {team.channel: 1}


def test_an_edit_does_not_add_a_non_viewer(team):
    message = send(team.alice_client, frame(team.sid, team.channel, doc(text())))
    hide_channel(team)

    edited = team.alice_client.patch(
        f"/messages/{message}", json={"content": doc(mention(team.bob.id), mention(99999))})

    assert edited.status_code == 200, edited.text
    assert stored_mentions(team.alice_client) == 0


@pytest.mark.parametrize("target", ["message", "forum_post", "channel"])
def test_deleting_removes_mentions(team, target):
    client = team.alice_client
    content = doc(mention(team.bob.id))
    if target == "forum_post":
        forum = create_channel(client, team.sid, "ideas", "forum")
        post_id, _ = post(client, forum["id"], content)
        delete = f"/posts/{post_id}"
    else:
        message = send(client, frame(team.sid, team.channel, content))
        delete = f"/messages/{message}" if target == "message" else f"/channels/{team.channel}"
    assert stored_mentions(client) == 1

    assert client.delete(delete).status_code == 204

    assert stored_mentions(client) == 0


@pytest.mark.parametrize("read, expected", [(None, 2), (0, 2), (1, 1), (2, 0)])
def test_mention_count_follows_the_marker(team, read, expected):
    sent = [
        send(team.alice_client, frame(team.sid, team.channel, content))
        for content in (doc(text()), doc(mention(team.bob.id)), doc(mention(team.bob.id)))]
    if read is not None:
        res = team.bob.client.put(
            f"/channels/{team.channel}/read", json={"message_id": sent[read]})
        assert res.status_code == 200, res.text

    assert counts(team.bob.client, team.sid) == {team.channel: expected}


def test_a_forum_channel_counts_every_post(team):
    forum = create_channel(team.alice_client, team.sid, "ideas", "forum")["id"]
    for _ in range(2):
        post(team.alice_client, forum, doc(mention(team.bob.id)))

    assert counts(team.bob.client, team.sid)[forum] == 2


def test_listing_carries_mention_count(team):
    other = create_channel(team.alice_client, team.sid, "other")["id"]
    send(team.alice_client, frame(team.sid, team.channel, doc(mention(team.bob.id))))

    listed = team.bob.client.get(f"/channels/{team.sid}")

    assert listed.status_code == 200, listed.text
    assert {c["id"]: c["mention_count"] for c in listed.json()} == {team.channel: 1, other: 0}


def test_a_regained_channel_frame_carries_the_count(team):
    send(team.alice_client, frame(team.sid, team.channel, doc(mention(team.bob.id))))
    path = hide_channel(team)

    with listening(team.alice_client, alice=team.alice_client, bob=team.bob.client) as live:
        assert overwrite(team.alice_client, path).status_code == 204
        frames = live.settle()

    created = of_type(frames["bob"], "channel_created")
    assert [f["channel"]["mention_count"] for f in created] == [1]


def test_mention_count_queries_do_not_grow(team):
    client = team.alice_client

    async def scenario():
        channels = [team.channel] + [
            (await Channel.create(server_id=team.sid, name=f"c{i}", type="text")).id
            for i in range(4)]
        users = [await User.create(username=f"reader{i}", password_hash="x") for i in range(20)]
        for channel_id in channels:
            message = await Message.create(
                uuid=uuid.uuid4(), content="x", author_id=team.alice["id"], server_id=team.sid,
                channel_id=channel_id, timestamp=datetime.now(timezone.utc))
            await MessageMention.bulk_create([
                MessageMention(message_id=message.id, user_id=user.id, channel_id=channel_id)
                for user in users])
        queries = {}
        for users_n, channels_n in ((2, 1), (20, 5)):
            with own_query_stats() as stats:
                result = await batch_mention_counts(
                    {user.id: channels[:channels_n] for user in users[:users_n]})
            assert {c for per_user in result.values() for c in per_user.values()} == {1}
            queries[(users_n, channels_n)] = stats.count
        return queries

    assert set(run(client, scenario).values()) == {1}
