"""Message history windows: `around` and `after` next to `before`, on channels,
forum posts and direct messages, and the `has_newer` flag every page carries."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.Message import Message
from tests.conftest import create_channel, create_server, register
from tests.test_direct_messages import open_conversation

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
KINDS = ["channel", "post", "direct"]
COUNT = 12


def at(seconds):
    return EPOCH + timedelta(seconds=seconds)


def doc(text):
    return {"type": "doc", "content": [{"type": "paragraph",
                                        "content": [{"type": "text", "text": text}]}]}


def seed(client, author_id, start, count, **where):
    """Insert messages whose arrival and own timestamps are `start..` seconds
    after EPOCH; returns their (uuid, row id) pairs oldest first."""
    async def create():
        rows = []
        for i in range(start, start + count):
            row = await Message.create(
                uuid=uuid.uuid4(), content=f"m{i}", author_id=author_id,
                timestamp=at(i), **where)
            await Message.filter(id=row.id).update(created_at=at(i))
            rows.append((str(row.uuid), row.id))
        return rows
    return client.portal.call(create)


def update_rows(client, row_ids, **values):
    async def update():
        await Message.filter(id__in=row_ids).update(**values)
    client.portal.call(update)


def delete_row(client, row_id):
    async def delete():
        await Message.filter(id=row_id).delete()
    client.portal.call(delete)


def make_post(client, channel_id, title):
    opening_id = str(uuid.uuid4())
    res = client.post(f"/channels/{channel_id}/posts", json={
        "title": title, "tag_ids": [],
        "message": {"id": opening_id, "content": doc(title), "timestamp": at(0).isoformat()}})
    assert res.status_code == 201, res.text
    post_id = res.json()["post"]["id"]

    async def pin_opening():
        row = await Message.get(post_id=post_id)
        await Message.filter(id=row.id).update(timestamp=at(0), created_at=at(0))
        return row.id
    return post_id, opening_id, client.portal.call(pin_opening)


@pytest.fixture
def world(client, new_client):
    """Three threads of COUNT messages each (m0 oldest), a stranger, and
    messages that live in other threads."""
    alice = register(client, "alice-window")
    author_id = alice["id"]
    server = create_server(client)
    sid = server["id"]
    general = create_channel(client, sid, "general")["id"]
    elsewhere = create_channel(client, sid, "elsewhere")["id"]
    forum = create_channel(client, sid, "ideas", "forum")["id"]
    post_id, opening_id, opening_row = make_post(client, forum, "post")
    other_post_id, other_post_message, _ = make_post(client, forum, "other post")

    bob_client = new_client()
    bob = register(bob_client, "bob-window")
    stranger = new_client()
    carol = register(stranger, "carol-window")
    with_bob = open_conversation(client, bob["id"])["id"]
    with_carol = open_conversation(client, carol["id"])["id"]

    channel_rows = seed(client, author_id, 0, COUNT, channel_id=general, server_id=sid)
    post_rows = [(opening_id, opening_row)] + seed(
        client, author_id, 1, COUNT - 1, channel_id=forum, server_id=sid, post_id=post_id)
    direct_rows = seed(client, author_id, 0, COUNT, conversation_id=with_bob)

    def thread(kind, url, rows, **params):
        return SimpleNamespace(
            kind=kind, client=client, url=url, params=params,
            ids=[uuid_ for uuid_, _ in rows], row_ids=[row for _, row in rows])

    return SimpleNamespace(
        stranger=stranger,
        threads={
            "channel": thread("channel", f"/channels/{general}/messages", channel_rows),
            "post": thread("post", f"/channels/{forum}/messages", post_rows, post_id=post_id),
            "direct": thread("direct", f"/conversations/{with_bob}/messages", direct_rows),
        },
        strays={
            "unknown": str(uuid.uuid4()),
            "other_channel": seed(
                client, author_id, 0, 1, channel_id=elsewhere, server_id=sid)[0][0],
            "other_post": other_post_message,
            "channel_message": channel_rows[6][0],
            "direct_message": direct_rows[6][0],
            "other_conversation": seed(client, author_id, 0, 1, conversation_id=with_carol)[0][0],
        },
    )


@pytest.fixture(params=KINDS)
def thread(request, world):
    return world.threads[request.param]


def get(thread, expect=200, client=None, **query):
    query = {key: value for key, value in query.items() if value is not None}
    res = (client or thread.client).get(thread.url, params={**thread.params, **query})
    assert res.status_code == expect, res.text
    return res.json()


def window(thread, indexes):
    return [thread.ids[i] for i in indexes]


def ids_of(page):
    return [m["id"] for m in page["messages"]]


AROUND = [
    pytest.param(6, 5, [8, 7, 6, 5, 4], True, True, id="middle"),
    pytest.param(0, 5, [2, 1, 0], False, True, id="oldest"),
    pytest.param(1, 5, [3, 2, 1, 0], False, True, id="second-oldest"),
    pytest.param(11, 5, [11, 10, 9], True, False, id="newest"),
    pytest.param(6, 1, [6], True, True, id="limit-one"),
    pytest.param(6, None, list(range(11, -1, -1)), False, False, id="default-limit"),
]


@pytest.mark.parametrize("target, limit, expected, has_more, has_newer", AROUND)
def test_around_returns_the_target_between_its_neighbours(
        thread, target, limit, expected, has_more, has_newer):
    page = get(thread, around=thread.ids[target], limit=limit)
    assert ids_of(page) == window(thread, expected)
    assert page["has_more"] is has_more
    assert page["has_newer"] is has_newer
    assert (page["next_cursor"] is not None) is has_more


AFTER = [
    pytest.param(3, [8, 7, 6, 5, 4], True, id="middle"),
    pytest.param(8, [11, 10, 9], False, id="reaches-the-newest"),
    pytest.param(11, [], False, id="nothing-newer"),
]


@pytest.mark.parametrize("anchor, expected, has_newer", AFTER)
def test_after_pages_towards_the_present(thread, anchor, expected, has_newer):
    page = get(thread, after=thread.ids[anchor], limit=5)
    assert ids_of(page) == window(thread, expected)
    assert page["has_newer"] is has_newer
    assert page["has_more"] is True

    oldest = expected[-1] if expected else anchor
    older = get(thread, before=page["next_cursor"], limit=1)
    assert ids_of(older) == window(thread, [oldest - 1])


def test_newest_and_before_pages_report_has_newer(thread):
    newest = get(thread, limit=5)
    assert newest["has_newer"] is False

    older = get(thread, before=newest["next_cursor"], limit=5)
    assert older["has_newer"] is True


def test_window_cursor_continues_backwards_without_a_gap(thread):
    page = get(thread, around=thread.ids[6], limit=5)
    older = get(thread, before=page["next_cursor"], limit=5)
    assert ids_of(older) == window(thread, [3, 2, 1, 0])
    assert older["has_more"] is False
    assert older["has_newer"] is True


def test_window_breaks_ties_on_id(thread):
    update_rows(thread.client, thread.row_ids[5:8], created_at=at(5), timestamp=at(5))

    page = get(thread, around=thread.ids[6], limit=5)
    assert ids_of(page) == window(thread, [8, 7, 6, 5, 4])

    page = get(thread, after=thread.ids[5], limit=5)
    assert ids_of(page) == window(thread, [10, 9, 8, 7, 6])


def test_pages_that_cross_equal_times_have_no_gap_or_repeat(thread):
    update_rows(thread.client, thread.row_ids[3:9], created_at=at(3), timestamp=at(3))

    walked, cursor = [], None
    while True:
        page = get(thread, before=cursor, limit=2)
        walked += ids_of(page)
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert walked == window(thread, range(COUNT - 1, -1, -1))

    walked, anchor = [], thread.ids[0]
    while True:
        page = get(thread, after=anchor, limit=2)
        if not page["messages"]:
            break
        walked += ids_of(page)[::-1]
        anchor = ids_of(page)[0]
    assert walked == window(thread, range(1, COUNT))


def test_post_window_follows_timestamp_not_arrival(world):
    thread = world.threads["post"]
    update_rows(thread.client, [thread.row_ids[3]], timestamp=at(8))
    update_rows(thread.client, [thread.row_ids[8]], timestamp=at(3))

    page = get(thread, around=thread.ids[6], limit=5)
    assert ids_of(page) == window(thread, [3, 7, 6, 5, 4])


@pytest.mark.parametrize("params", [
    pytest.param(("before", "around"), id="before-around"),
    pytest.param(("around", "after"), id="around-after"),
    pytest.param(("before", "around", "after"), id="all-three"),
])
def test_only_one_of_before_around_after(thread, params):
    query = {name: thread.ids[6] if name != "before" else "cursor" for name in params}
    res = thread.client.get(thread.url, params={**thread.params, **query})
    assert res.status_code == 422
    assert res.json()["detail"] == "Use only one of before, around and after"


OUTSIDE = [
    pytest.param("channel", "unknown", id="channel-unknown"),
    pytest.param("channel", "other_channel", id="channel-other-channel"),
    pytest.param("channel", "direct_message", id="channel-direct-message"),
    pytest.param("channel", "deleted", id="channel-deleted"),
    pytest.param("post", "unknown", id="post-unknown"),
    pytest.param("post", "other_post", id="post-other-post"),
    pytest.param("post", "channel_message", id="post-channel-message"),
    pytest.param("post", "deleted", id="post-deleted"),
    pytest.param("direct", "unknown", id="direct-unknown"),
    pytest.param("direct", "channel_message", id="direct-channel-message"),
    pytest.param("direct", "other_conversation", id="direct-other-conversation"),
    pytest.param("direct", "deleted", id="direct-deleted"),
]


@pytest.mark.parametrize("param", ["around", "after"])
@pytest.mark.parametrize("kind, target", OUTSIDE)
def test_window_target_outside_the_thread_is_404(world, kind, target, param):
    thread = world.threads[kind]
    if target == "deleted":
        delete_row(thread.client, thread.row_ids[6])
        message_id = thread.ids[6]
    else:
        message_id = world.strays[target]

    res = thread.client.get(thread.url, params={**thread.params, param: message_id})
    assert res.status_code == 404
    assert res.json()["detail"] == "Message not found"


@pytest.mark.parametrize("param", ["around", "after"])
def test_malformed_window_target_is_422(thread, param):
    res = thread.client.get(thread.url, params={**thread.params, param: "not-a-uuid"})
    assert res.status_code == 422


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "malformed"])
@pytest.mark.parametrize("param", ["around", "after"])
def test_window_checks_access_first(thread, world, param, valid):
    target = thread.ids[6] if valid else "not-a-uuid"
    res = world.stranger.get(thread.url, params={**thread.params, param: target})
    if thread.kind == "direct":
        assert res.status_code == 404
        assert res.json()["detail"] == "Conversation not found"
    else:
        assert res.status_code == 403
