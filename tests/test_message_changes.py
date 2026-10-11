"""The feed of edited and deleted messages, and the lookup of messages by id."""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.Message import Message
from app.models.MessageTombstone import MessageTombstone
from app.services.message_changes import (
    BEFORE_ROWS,
    FeedPosition,
    decode,
    encode,
    prune_tombstones,
)
from tests.conftest import create_channel, create_server, register
from tests.test_channel_permissions import crew, doc  # noqa: F401  (crew is a fixture)
from tests.test_direct_messages import open_conversation
from tests.test_forum import make_post, reply, team  # noqa: F401  (team is a fixture)
from tests.test_permissions import join, set_roles

EDITED_DOC = doc("edited")


def run(client, awaitable):
    async def wait():
        return await awaitable
    return client.portal.call(wait)


def utc_now():
    return datetime.now(timezone.utc)


def add_message(client, author_id, *, server_id=None, channel_id=None, conversation_id=None,
                reply_to=None) -> str:
    message_id = uuid.uuid4()
    run(client, Message.create(
        uuid=message_id, content=json.dumps(doc("original")), author_id=author_id,
        server_id=server_id, channel_id=channel_id, conversation_id=conversation_id,
        timestamp=utc_now(), reply_to_uuid=reply_to))
    return str(message_id)


def backdate(client, message_id, by=timedelta(minutes=2)):
    """Move a message's edit and its tombstone, if it has either, into the past."""
    when = utc_now() - by
    run(client, Message.filter(uuid=message_id).update(edited_at=when))
    run(client, MessageTombstone.filter(message_uuid=message_id).update(deleted_at=when))


def edit(client, message_id):
    res = client.patch(f"/messages/{message_id}", json={"content": EDITED_DOC})
    assert res.status_code == 200, res.text
    return res.json()


def delete(client, message_id):
    assert client.delete(f"/messages/{message_id}").status_code == 204


CHANGES = {"edited": edit, "deleted": delete}


def feed(client, since=None, **params):
    if since is not None:
        params["since"] = since
    res = client.get("/messages/changes", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def cursor_of(client):
    return feed(client)["cursor"]


def changes_since(client, cursor):
    page = feed(client, cursor)
    assert not page["has_more"]
    return {(c["id"], c["kind"]) for c in page["changes"]}


def walk(client, cursor, limit):
    changes = []
    for _ in range(50):
        page = feed(client, cursor, limit=limit)
        changes += page["changes"]
        cursor = page["cursor"]
        if not page["has_more"]:
            return changes, cursor
    raise AssertionError("the walk never ended")


def lookup(client, ids):
    res = client.get("/messages", params={"ids": ids})
    assert res.status_code == 200, res.text
    return res.json()["messages"]


def token(**fields):
    body = {"v": 1, "t": utc_now().isoformat(), "k": BEFORE_ROWS, "i": 0, "f": None, **fields}
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode()


@pytest.fixture
def room(client, new_client):
    """An owner (the `client`), bob and carol in one server with one channel."""
    owner = register(client, "owner-feed")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client, bob = join(client, new_client, server["id"])
    carol_client, carol = join(client, new_client, server["id"])
    return SimpleNamespace(
        owner=client, owner_user=owner, sid=server["id"], cid=channel["id"],
        bob=bob_client, bob_user=bob, carol=carol_client, carol_user=carol)


def channel_message(room, author_user):
    return add_message(room.owner, author_user["id"], server_id=room.sid, channel_id=room.cid)


# -- the feed ---------------------------------------------------------------

def test_without_since_only_a_fresh_cursor_comes_back(room):
    page = feed(room.carol)

    assert page["reset"] is False
    assert page["changes"] == []
    assert page["has_more"] is False
    position = decode(page["cursor"])
    assert utc_now() - position.at < timedelta(minutes=1)
    assert position.kind == BEFORE_ROWS


@pytest.mark.parametrize("kind", ["edited", "deleted"])
def test_changes_after_the_cursor_are_listed_and_earlier_ones_are_not(room, kind):
    old = channel_message(room, room.bob_user)
    CHANGES[kind](room.bob, old)
    backdate(room.owner, old)
    cursor = cursor_of(room.carol)
    new = channel_message(room, room.bob_user)
    CHANGES[kind](room.bob, new)

    page = feed(room.carol, cursor)

    assert [(c["id"], c["kind"]) for c in page["changes"]] == [(new, kind)]
    assert datetime.fromisoformat(page["changes"][0]["at"]).tzinfo is not None


@pytest.mark.parametrize("kind", ["edited", "deleted"])
def test_both_people_in_a_dm_see_its_changes(client, new_client, kind):
    alice = register(client, "alice-feed")
    bob_client = new_client()
    bob = register(bob_client, "bob-feed")
    convo = open_conversation(client, bob["id"])
    mine = add_message(client, alice["id"], conversation_id=convo["id"])
    cursors = [cursor_of(client), cursor_of(bob_client)]

    CHANGES[kind](client, mine)

    for who, cursor in zip((client, bob_client), cursors):
        assert changes_since(who, cursor) == {(mine, kind)}


def hide_in_private_channel(crew):
    message = add_message(
        crew.owner_client, crew.owner["id"], server_id=crew.sid, channel_id=crew.secret["id"])
    return crew.member_client, message


def hide_by_removing_the_role(crew):
    message = add_message(
        crew.owner_client, crew.owner["id"], server_id=crew.sid, channel_id=crew.secret["id"])
    viewer = crew.mod_client
    assert message in {m["id"] for m in lookup(viewer, [message])}
    assert set_roles(crew.owner_client, crew.sid, crew.mod["id"], []).status_code == 200
    return viewer, message


def hide_a_dm(crew):
    convo = open_conversation(crew.owner_client, crew.mod["id"])
    message = add_message(crew.owner_client, crew.owner["id"], conversation_id=convo["id"])
    return crew.member_client, message


@pytest.mark.parametrize("kind", ["edited", "deleted"])
@pytest.mark.parametrize("setup", [
    pytest.param(hide_in_private_channel, id="private channel"),
    pytest.param(hide_by_removing_the_role, id="member lost access"),
    pytest.param(hide_a_dm, id="someone else's DM"),
])
def test_changes_the_caller_cant_view_are_left_out(crew, setup, kind):
    viewer, hidden = setup(crew)
    visible = add_message(
        crew.owner_client, crew.owner["id"], server_id=crew.sid, channel_id=crew.general["id"])
    cursor = cursor_of(viewer)

    CHANGES[kind](crew.owner_client, hidden)
    CHANGES[kind](crew.owner_client, visible)

    assert changes_since(viewer, cursor) == {(visible, kind)}


def test_forum_post_changes_follow_the_channel(team):  # noqa: F811
    post = make_post(team.author, team.forum)
    opening_id = post["message"]["id"]
    reply_id = reply(team.author, team, post["post"]["id"])
    cursor = cursor_of(team.member)

    edit(team.author, opening_id)
    delete(team.author, reply_id)

    assert changes_since(team.member, cursor) == {(opening_id, "edited"), (reply_id, "deleted")}


def test_a_message_edited_and_then_deleted_is_only_a_delete(room):
    message = channel_message(room, room.bob_user)
    cursor = cursor_of(room.carol)

    edit(room.bob, message)
    delete(room.bob, message)

    assert changes_since(room.carol, cursor) == {(message, "deleted")}


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_a_walk_over_equal_timestamps_lists_each_change_once(room, limit):
    cursor = cursor_of(room.carol)
    moment = utc_now()
    edits = [channel_message(room, room.bob_user) for _ in range(3)]
    run(room.owner, Message.filter(uuid__in=edits).update(edited_at=moment))
    deletes = [uuid.uuid4() for _ in range(2)]
    for message_uuid in deletes:
        run(room.owner, MessageTombstone.create(
            message_uuid=message_uuid, server_id=room.sid, channel_id=room.cid,
            deleted_at=moment))

    changes, end = walk(room.carol, cursor, limit)

    assert [c["kind"] for c in changes] == ["edited"] * 3 + ["deleted"] * 2
    assert {c["id"] for c in changes[:3]} == set(edits)
    assert {c["id"] for c in changes[3:]} == {str(u) for u in deletes}
    assert decode(end).kind == BEFORE_ROWS


def test_an_idle_walk_moves_the_cursor_forward(room):
    first = cursor_of(room.carol)

    page = feed(room.carol, first)

    assert page["changes"] == []
    assert decode(page["cursor"]).at > decode(first).at


def test_a_cursor_older_than_the_retention_resets(room):
    stale = encode(FeedPosition(utc_now() - timedelta(days=31), BEFORE_ROWS, 0))

    page = feed(room.carol, stale)

    assert page["reset"] is True
    assert page["changes"] == []
    assert feed(room.carol, page["cursor"])["reset"] is False


@pytest.mark.parametrize("since", [
    pytest.param("not a cursor!", id="garbage"),
    pytest.param(base64.urlsafe_b64encode(b"nope").decode(), id="not json"),
    pytest.param(token(v=2), id="wrong version"),
    pytest.param(token(t="2026-01-01T00:00:00"), id="naive time"),
    pytest.param(token(k=7), id="unknown kind"),
    pytest.param(token(i=-1), id="negative row"),
])
def test_a_cursor_that_does_not_decode_is_400(room, since):
    res = room.carol.get("/messages/changes", params={"since": since})

    assert res.status_code == 400
    assert res.json()["detail"] == "Invalid cursor"


def test_limit_is_clamped(room):
    cursor = cursor_of(room.carol)
    for _ in range(2):
        edit(room.bob, channel_message(room, room.bob_user))

    assert len(feed(room.carol, cursor, limit=0)["changes"]) == 1
    assert len(feed(room.carol, cursor, limit=100000)["changes"]) == 2


@pytest.mark.parametrize("deleter, author", [
    pytest.param("bob", "bob", id="author"),
    pytest.param("owner", "bob", id="moderator"),
])
def test_author_and_moderator_deletes_leave_a_tombstone(room, deleter, author):
    message = channel_message(room, room.bob_user)
    cursor = cursor_of(room.carol)

    delete(getattr(room, deleter), message)

    assert changes_since(room.carol, cursor) == {(message, "deleted")}
    tombstone = run(room.owner, MessageTombstone.get(message_uuid=message))
    assert (tombstone.server_id, tombstone.channel_id) == (room.sid, room.cid)


def test_deleting_an_account_leaves_tombstones_for_its_channel_messages(room, client):
    mine = [channel_message(room, room.bob_user) for _ in range(2)]
    theirs = channel_message(room, room.carol_user)
    convo = open_conversation(room.bob, room.carol_user["id"])
    in_dm = add_message(room.bob, room.bob_user["id"], conversation_id=convo["id"])
    cursor = cursor_of(room.carol)

    assert room.bob.delete(f"/users/{room.bob_user['id']}").status_code == 204

    assert changes_since(room.carol, cursor) == {(m, "deleted") for m in mine}
    assert not run(client, MessageTombstone.filter(message_uuid=in_dm).exists())
    assert run(client, Message.filter(uuid=theirs).exists())


def test_pruning_drops_only_expired_tombstones(client):
    now = utc_now()
    for age in (timedelta(days=31), timedelta(days=29), timedelta()):
        run(client, MessageTombstone.create(message_uuid=uuid.uuid4(), deleted_at=now - age))

    assert run(client, prune_tombstones()) == 1
    assert run(client, MessageTombstone.all().count()) == 2


# -- the lookup by ids ------------------------------------------------------

def test_lookup_returns_the_message_updated_shape(room):
    original = channel_message(room, room.bob_user)
    message = add_message(
        room.owner, room.bob_user["id"], server_id=room.sid, channel_id=room.cid,
        reply_to=uuid.UUID(original))
    assert room.carol.put(f"/messages/{message}/reactions/👍").status_code == 204
    edited = edit(room.bob, message)

    items = lookup(room.carol, [message, str(uuid.uuid4())])

    assert items == [edited]
    assert set(items[0]) == {
        "id", "server_id", "channel_id", "post_id", "conversation_id", "user_id", "content",
        "timestamp", "edited_at", "attachments", "reactions", "reply_to", "embeds",
        "imported_author"}
    assert items[0]["reactions"] and items[0]["reply_to"]["id"] == original


def test_lookup_hides_what_the_caller_cant_view(crew):
    secret, dm_id = hide_in_private_channel(crew)[1], hide_a_dm(crew)[1]
    open_message = add_message(
        crew.owner_client, crew.owner["id"], server_id=crew.sid, channel_id=crew.general["id"])

    seen = lookup(crew.member_client, [secret, dm_id, open_message])

    assert [m["id"] for m in seen] == [open_message]
    assert {m["id"] for m in lookup(crew.owner_client, [secret, dm_id, open_message])} == {
        secret, dm_id, open_message}


@pytest.mark.parametrize("count, status", [(0, 200), (100, 200), (101, 422)])
def test_lookup_takes_up_to_100_ids(room, count, status):
    res = room.carol.get("/messages", params={"ids": [str(uuid.uuid4()) for _ in range(count)]})

    assert res.status_code == status
    if status == 200:
        assert res.json() == {"messages": []}


def test_lookup_rejects_a_malformed_id(room):
    assert room.carol.get("/messages", params={"ids": "not-a-uuid"}).status_code == 422


def test_lookup_ignores_duplicate_ids(room):
    message = channel_message(room, room.bob_user)

    assert [m["id"] for m in lookup(room.carol, [message, message])] == [message]
