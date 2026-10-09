"""Forum channels: posts, the one message pipeline, history order, the index,
permissions, locking and deletion."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.models.Message import Message
from app.services import forum as forum_service
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_permissions import join, promote

HEADERS = {"origin": ORIGIN}
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def doc(text):
    return {"type": "doc", "content": [{"type": "paragraph",
                                        "content": [{"type": "text", "text": text}]}]}


def text_of(message):
    return json.loads(message["content"])["content"][0]["content"][0]["text"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def run(client, awaitable):
    async def wait():
        return await awaitable
    return client.portal.call(wait)


@pytest.fixture(autouse=True)
def attachments_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    return tmp_path / "attachments"


@pytest.fixture
def team(client, new_client):
    """An owner, a moderator (Admin role), an author and a plain member."""
    register(client, "owner-forum")
    server = create_server(client)
    forum = create_channel(client, server["id"], "ideas", "forum")
    other_forum = create_channel(client, server["id"], "ideas-2", "forum")
    text = create_channel(client, server["id"], "chat")
    mod_client, mod = join(client, new_client, server["id"])
    promote(client, server["id"], mod["id"])
    author_client, author = join(client, new_client, server["id"])
    member_client, member = join(client, new_client, server["id"])
    return SimpleNamespace(
        owner=client, sid=server["id"], forum=forum["id"], other_forum=other_forum["id"],
        text=text["id"], mod=mod_client, mod_user=mod, author=author_client,
        author_user=author, member=member_client, member_user=member)


def opening(text="opening", **overrides):
    message = {
        "id": str(uuid.uuid4()),
        "content": doc(text),
        "timestamp": now_iso(),
        **overrides,
    }
    return message


def new_post(client, channel_id, title="First post", tag_ids=(), message=None):
    return client.post(f"/channels/{channel_id}/posts", json={
        "title": title, "tag_ids": list(tag_ids), "message": message or opening()})


def make_post(client, channel_id, **kwargs) -> dict:
    res = new_post(client, channel_id, **kwargs)
    assert res.status_code == 201, res.text
    return res.json()


def reply_frame(team, post_id, text="reply", *, channel_id=None, timestamp=None, **extra):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": team.sid,
        "channel_id": channel_id or team.forum,
        "post_id": post_id,
        "content": doc(text),
        "timestamp": timestamp or now_iso(),
        **extra,
    }


def recv(ws, wanted_type):
    for _ in range(12):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


def reply(client, team, post_id, text="reply", **kwargs):
    """Send a reply over a short-lived socket and return the stored message id."""
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        frame = reply_frame(team, post_id, text, **kwargs)
        ws.send_json(frame)
        assert recv(ws, "message_ack")["id"] == frame["id"]
    return frame["id"]


def rejected(client, frame):
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(frame)
        return recv(ws, "error")


def post_history(client, channel_id, post_id, **params):
    res = client.get(f"/channels/{channel_id}/messages", params={"post_id": post_id, **params})
    assert res.status_code == 200, res.text
    return res.json()


def index(client, channel_id, **params):
    res = client.get(f"/channels/{channel_id}/posts", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def get_post(client, channel_id, post_id):
    res = client.get(f"/channels/{channel_id}/posts/{post_id}")
    assert res.status_code == 200, res.text
    return res.json()


def stored_messages(client):
    return run(client, Message.all().count())


def create_tag(client, channel_id, name, color=None):
    res = client.post(f"/channels/{channel_id}/tags", json={"name": name, "color": color})
    assert res.status_code == 201, res.text
    return res.json()


def upload(client, channel_id, *, name="pic.png", content_type="image/png", data=None):
    res = client.post(
        "/attachments",
        files={"file": (name, png_bytes() if data is None else data, content_type)},
        data={"channel_id": str(channel_id)})
    assert res.status_code == 201, res.text
    return res.json()


# -- creating a post -------------------------------------------------------

def test_post_create_returns_row_and_opening_message(team):
    tag = create_tag(team.owner, team.forum, "idea")
    message = opening("hello forum")

    res = new_post(team.author, team.forum, "My idea", [tag["id"]], message)

    assert res.status_code == 201, res.text
    body = res.json()
    post, sent = body["post"], body["message"]
    assert post["title"] == "My idea"
    assert post["channel_id"] == team.forum
    assert post["author_id"] == team.author_user["id"]
    assert post["tag_ids"] == [tag["id"]]
    assert (post["pinned"], post["locked"], post["reply_count"]) == (False, False, 0)
    assert post["thumbnail"] is None
    assert sent["id"] == message["id"]
    assert sent["post_id"] == post["id"]
    assert sent["channel_id"] == team.forum
    assert sent["user_id"] == team.author_user["id"]
    assert sent["content"] == message["content"]

    assert index(team.member, team.forum)["posts"] == [post]
    detail = get_post(team.member, team.forum, post["id"])
    assert detail == {**post, "opening_message_id": message["id"]}
    history = post_history(team.member, team.forum, post["id"])["messages"]
    assert [m["id"] for m in history] == [message["id"]]
    assert history[0]["post_id"] == post["id"]


def test_post_create_reaches_other_members_live(team):
    with team.member.websocket_connect("/ws", headers=HEADERS) as member_ws:
        ws_ready(member_ws)
        body = make_post(team.author, team.forum, title="Live")

        created = recv(member_ws, "forum_post_created")
        message = recv(member_ws, "message")

    assert created == {
        "type": "forum_post_created", "server_id": team.sid,
        "channel_id": team.forum, "post": body["post"]}
    assert message["id"] == body["message"]["id"]
    assert message["post_id"] == body["post"]["id"]


def test_post_create_validates_the_request(team):
    assert new_post(team.author, team.forum, title="   ").status_code == 422
    assert new_post(team.author, team.forum, title="x" * 201).status_code == 422
    assert new_post(team.author, team.forum, message=opening(content="")).status_code == 422
    assert new_post(team.author, team.forum, message=opening(id="nope")).status_code == 422
    assert new_post(team.author, team.forum, message=opening(timestamp="soon")).status_code == 422
    assert new_post(team.author, team.forum, tag_ids=[999]).status_code == 422
    assert index(team.owner, team.forum)["posts"] == []
    assert stored_messages(team.owner) == 0


def test_post_create_with_a_taken_message_id_is_a_conflict(team):
    message = opening()
    make_post(team.author, team.forum, message=message)

    assert new_post(team.author, team.forum, message=message).status_code == 409
    assert len(index(team.owner, team.forum)["posts"]) == 1


def test_post_create_needs_send_permission_and_membership(team, new_client):
    outsider = new_client()
    register(outsider)
    assert new_post(outsider, team.forum).status_code == 403
    assert outsider.get(f"/channels/{team.forum}/posts").status_code == 403
    assert new_post(team.owner, 99999).status_code == 404


def test_post_create_on_a_text_channel_is_rejected(team):
    res = new_post(team.author, team.text)

    assert res.status_code == 422
    assert stored_messages(team.owner) == 0
    assert team.author.get(f"/channels/{team.text}/posts").status_code == 422


def test_opening_message_carries_attachments_and_embeds(team):
    attachment = upload(team.author, team.forum)
    embed = {"url": "https://example.com/a", "title": "A"}

    body = make_post(team.author, team.forum, message=opening(
        content=None, attachment_ids=[attachment["id"]], embeds=[embed]))

    assert body["message"]["attachments"][0]["id"] == attachment["id"]
    assert body["message"]["embeds"][0]["url"] == embed["url"]
    history = post_history(team.member, team.forum, body["post"]["id"])["messages"]
    assert history[0]["attachments"][0]["id"] == attachment["id"]
    assert history[0]["embeds"][0]["url"] == embed["url"]


def test_a_rejected_opening_message_creates_no_post(team):
    res = new_post(team.author, team.forum, message=opening(attachment_ids=[str(uuid.uuid4())]))

    assert res.status_code == 422
    assert res.json()["detail"] == "invalid_attachments"
    assert index(team.owner, team.forum)["posts"] == []
    assert stored_messages(team.owner) == 0


# -- the pipeline invariants -----------------------------------------------

def test_ws_message_to_a_forum_without_post_id_is_invalid_post(team):
    make_post(team.author, team.forum)
    frame = reply_frame(team, None)

    assert rejected(team.member, frame) == {
        "type": "error", "code": "invalid_post", "ref": frame["id"]}
    assert stored_messages(team.owner) == 1


def test_ws_message_with_post_id_to_a_text_channel_is_invalid_post(team):
    post = make_post(team.author, team.forum)["post"]
    frame = reply_frame(team, post["id"], channel_id=team.text)

    assert rejected(team.member, frame) == {
        "type": "error", "code": "invalid_post", "ref": frame["id"]}
    assert team.member.get(f"/channels/{team.text}/messages").json()["messages"] == []
    assert stored_messages(team.owner) == 1


def test_post_of_another_forum_is_invalid_post(team):
    post = make_post(team.author, team.forum)["post"]
    frame = reply_frame(team, post["id"], channel_id=team.other_forum)

    assert rejected(team.member, frame)["code"] == "invalid_post"
    unknown = reply_frame(team, 99999)
    assert rejected(team.member, unknown)["code"] == "invalid_post"
    assert get_post(team.member, team.forum, post["id"])["reply_count"] == 0
    assert stored_messages(team.owner) == 1


def test_a_failed_reply_leaves_the_post_untouched(team):
    post = make_post(team.author, team.forum)["post"]
    frame = reply_frame(team, post["id"], reply_to=str(uuid.uuid4()))

    assert rejected(team.member, frame)["code"] == "invalid_reply"
    after = get_post(team.member, team.forum, post["id"])
    assert (after["reply_count"], after["last_activity_at"]) == (
        0, post["last_activity_at"])


def test_typing_in_a_post_is_relayed_with_post_id(team):
    post = make_post(team.author, team.forum)["post"]
    typing = {"type": "typing", "server_id": team.sid, "channel_id": team.forum,
              "post_id": post["id"]}
    with team.member.websocket_connect("/ws", headers=HEADERS) as member_ws, \
            team.author.websocket_connect("/ws", headers=HEADERS) as author_ws:
        ws_ready(member_ws)
        ws_ready(author_ws)
        member_ws.send_json(typing)
        assert recv(author_ws, "typing") == {
            "type": "typing", "user_id": team.member_user["id"], "server_id": team.sid,
            "channel_id": team.forum, "post_id": post["id"]}

        # Without a post, or with a post in a text channel, it is dropped.
        author_ws.send_json({**typing, "post_id": None})
        author_ws.send_json({**typing, "channel_id": team.text})
        author_ws.send_json({"type": "ping", "t": 1})
        assert author_ws.receive_json() == {"type": "pong", "t": 1}


# -- replies ---------------------------------------------------------------

def test_reply_bumps_the_post_and_notifies_members(team):
    body = make_post(team.author, team.forum)
    post = body["post"]
    with team.member.websocket_connect("/ws", headers=HEADERS) as member_ws, \
            team.author.websocket_connect("/ws", headers=HEADERS) as author_ws:
        ws_ready(member_ws)
        ws_ready(author_ws)
        frame = reply_frame(team, post["id"], "a reply")
        author_ws.send_json(frame)

        message = recv(member_ws, "message")
        updated = recv(member_ws, "forum_post_updated")
        sender_updated = recv(author_ws, "forum_post_updated")

    assert message["id"] == frame["id"]
    assert message["post_id"] == post["id"]
    assert updated == sender_updated
    assert updated["channel_id"] == team.forum
    assert updated["post"]["reply_count"] == 1
    assert updated["post"]["last_activity_at"] > post["last_activity_at"]
    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 1


def test_post_history_is_ordered_by_timestamp_not_arrival(team):
    post = make_post(team.author, team.forum, message=opening(
        timestamp=(EPOCH + timedelta(minutes=10)).isoformat()))["post"]
    stamps = {"late": 50, "early": 20, "middle": 30}
    for name, minutes in stamps.items():
        reply(team.member, team, post["id"], name,
              timestamp=(EPOCH + timedelta(minutes=minutes)).isoformat())

    page = post_history(team.owner, team.forum, post["id"])

    texts = [text_of(m) for m in page["messages"]]
    assert texts == ["late", "middle", "early", "opening"]


def test_post_history_ties_break_on_id_and_pages_with_a_cursor(team):
    post = make_post(team.author, team.forum, message=opening(
        timestamp=EPOCH.isoformat()))["post"]
    same = (EPOCH + timedelta(minutes=5)).isoformat()
    ids = [reply(team.member, team, post["id"], f"r{i}", timestamp=same) for i in range(3)]

    first = post_history(team.owner, team.forum, post["id"], limit=2)
    assert [m["id"] for m in first["messages"]] == [ids[2], ids[1]]
    assert first["has_more"] is True
    second = post_history(
        team.owner, team.forum, post["id"], limit=2, before=first["next_cursor"])
    assert [m["id"] for m in second["messages"]][0] == ids[0]
    assert len(second["messages"]) == 2
    assert second["has_more"] is False and second["next_cursor"] is None


def test_history_needs_post_id_exactly_in_forum_channels(team):
    post = make_post(team.author, team.forum)["post"]

    assert team.owner.get(f"/channels/{team.forum}/messages").status_code == 422
    assert team.owner.get(
        f"/channels/{team.text}/messages", params={"post_id": post["id"]}).status_code == 422
    assert team.owner.get(
        f"/channels/{team.other_forum}/messages", params={"post_id": post["id"]}).status_code == 404
    assert team.owner.get(f"/channels/{team.text}/messages").status_code == 200


def test_quote_reply_stays_inside_the_post(team):
    first = make_post(team.author, team.forum, message=opening("one"))
    second = make_post(team.author, team.forum, message=opening("two"))
    first_id, second_id = first["post"]["id"], second["post"]["id"]

    across = reply_frame(team, second_id, reply_to=first["message"]["id"])
    assert rejected(team.member, across)["code"] == "invalid_reply"
    assert get_post(team.owner, team.forum, second_id)["reply_count"] == 0

    inside_id = reply(team.member, team, first_id, "quoting", reply_to=first["message"]["id"])
    history = post_history(team.owner, team.forum, first_id)["messages"]
    quoting = next(m for m in history if m["id"] == inside_id)
    assert quoting["reply_to"]["id"] == first["message"]["id"]
    assert quoting["reply_to"]["preview"] == "one"


def test_reactions_edit_and_delete_work_on_replies(team):
    post = make_post(team.author, team.forum)["post"]
    reply_id = reply(team.member, team, post["id"], "typo")

    assert team.author.put(f"/messages/{reply_id}/reactions/%F0%9F%91%8D").status_code == 204
    edited = team.member.patch(f"/messages/{reply_id}", json={"content": doc("fixed")})
    assert edited.status_code == 200, edited.text
    assert edited.json()["post_id"] == post["id"]
    stored = post_history(team.owner, team.forum, post["id"])["messages"][0]
    assert stored["reactions"][0]["emoji"] == "👍"
    assert text_of(stored) == "fixed"

    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 1
    with team.author.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert team.member.delete(f"/messages/{reply_id}").status_code == 204
        deleted = recv(ws, "message_deleted")
        updated = recv(ws, "forum_post_updated")
    assert deleted["post_id"] == post["id"]
    assert updated["post"]["reply_count"] == 0
    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 0


def test_the_opening_message_cannot_be_deleted(team):
    body = make_post(team.author, team.forum)

    res = team.author.delete(f"/messages/{body['message']['id']}")

    assert res.status_code == 409
    assert team.mod.delete(f"/messages/{body['message']['id']}").status_code == 409
    assert len(post_history(team.owner, team.forum, body["post"]["id"])["messages"]) == 1


def test_editing_the_opening_message_works(team):
    body = make_post(team.author, team.forum)

    res = team.author.patch(
        f"/messages/{body['message']['id']}", json={"content": doc("edited")})

    assert res.status_code == 200
    assert res.json()["post_id"] == body["post"]["id"]
    assert get_post(team.owner, team.forum, body["post"]["id"])["title"] == "First post"


# -- the index -------------------------------------------------------------

def test_tag_filter_requires_every_tag(team):
    a, b, c = (create_tag(team.owner, team.forum, name) for name in ("a", "b", "c"))
    make_post(team.author, team.forum, title="both", tag_ids=[a["id"], b["id"]])
    make_post(team.author, team.forum, title="only a", tag_ids=[a["id"]])
    make_post(team.author, team.forum, title="only b", tag_ids=[b["id"]])
    make_post(team.author, team.forum, title="none")

    def titles(*tags):
        page = index(team.member, team.forum, tag_ids=[t["id"] for t in tags])
        return [p["title"] for p in page["posts"]]

    assert titles(a, b) == ["both"]
    assert titles(a) == ["only a", "both"]
    assert titles(b) == ["only b", "both"]
    assert titles(a, b, c) == []
    assert titles() == ["none", "only b", "only a", "both"]


def test_tag_filter_rejects_unknown_tags(team):
    foreign = create_tag(team.owner, team.other_forum, "elsewhere")
    known = create_tag(team.owner, team.forum, "here")

    for bad in ([999], [foreign["id"]], [known["id"], foreign["id"]]):
        res = team.member.get(f"/channels/{team.forum}/posts", params={"tag_ids": bad})
        assert res.status_code == 422, bad


def test_pinned_posts_come_first_on_the_first_page_only(team, monkeypatch):
    monkeypatch.setattr(forum_service, "POST_PAGE_SIZE", 2)
    posts = [make_post(team.author, team.forum, title=f"p{i}")["post"] for i in range(5)]
    pin = team.mod.patch(f"/posts/{posts[0]['id']}", json={"pinned": True})
    assert pin.status_code == 200

    first = index(team.member, team.forum)
    assert [p["title"] for p in first["posts"]] == ["p0", "p4", "p3"]
    second = index(team.member, team.forum, cursor=first["next_cursor"])
    assert [p["title"] for p in second["posts"]] == ["p2", "p1"]
    assert second["next_cursor"] is None


def test_cursor_pagination_walks_every_post_once(team, monkeypatch):
    monkeypatch.setattr(forum_service, "POST_PAGE_SIZE", 2)
    for i in range(5):
        make_post(team.author, team.forum, title=f"p{i}")

    seen, cursor, pages = [], None, 0
    while True:
        page = index(team.member, team.forum, **({"cursor": cursor} if cursor else {}))
        seen += [p["title"] for p in page["posts"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == ["p4", "p3", "p2", "p1", "p0"]
    assert pages == 3
    assert team.member.get(
        f"/channels/{team.forum}/posts", params={"cursor": "junk"}).status_code == 400


def test_a_reply_moves_the_post_to_the_top(team):
    old = make_post(team.author, team.forum, title="old")["post"]
    make_post(team.author, team.forum, title="new")

    reply(team.member, team, old["id"])

    assert [p["title"] for p in index(team.owner, team.forum)["posts"]] == ["old", "new"]


def thumbnail_of(team, **opening_fields):
    post = make_post(team.author, team.forum, message=opening(**opening_fields))["post"]
    return next(p for p in index(team.owner, team.forum)["posts"] if p["id"] == post["id"])[
        "thumbnail"], post


EMBED_WITH_IMAGE = {"url": "https://example.com/a", "image_url": "https://example.com/i.png"}
EMBED_WITHOUT_IMAGE = {"url": "https://example.com/b", "title": "B"}


def test_thumbnail_is_the_first_image_attachment(team):
    image = upload(team.author, team.forum)
    video = upload(team.author, team.forum, name="clip.mp4", content_type="video/mp4",
                   data=b"\x00\x00\x00\x18ftypmp42")

    thumbnail, _ = thumbnail_of(
        team, attachment_ids=[video["id"], image["id"]], embeds=[EMBED_WITH_IMAGE])

    assert thumbnail == {"type": "image", "attachment": image}


def test_thumbnail_falls_back_to_the_link_preview_image(team):
    video = upload(team.author, team.forum, name="clip.mp4", content_type="video/mp4",
                   data=b"\x00\x00\x00\x18ftypmp42")

    thumbnail, _ = thumbnail_of(
        team, attachment_ids=[video["id"]], embeds=[EMBED_WITH_IMAGE])

    assert thumbnail == {"type": "embed", "url": EMBED_WITH_IMAGE["image_url"]}


def test_thumbnail_then_a_video_attachment_for_a_client_poster(team):
    video = upload(team.author, team.forum, name="clip.mp4", content_type="video/mp4",
                   data=b"\x00\x00\x00\x18ftypmp42")

    thumbnail, _ = thumbnail_of(
        team, attachment_ids=[video["id"]], embeds=[EMBED_WITHOUT_IMAGE])

    assert thumbnail == {"type": "video", "attachment": video}


def test_thumbnail_is_none_without_images_or_videos(team):
    document = upload(team.author, team.forum, name="a.txt", content_type="text/plain",
                      data=b"hello")

    assert thumbnail_of(team)[0] is None
    assert thumbnail_of(team, attachment_ids=[document["id"]])[0] is None
    assert thumbnail_of(team, embeds=[EMBED_WITHOUT_IMAGE])[0] is None


# -- permissions -----------------------------------------------------------

def test_author_edits_own_title_and_tags(team):
    tag = create_tag(team.owner, team.forum, "t")
    post = make_post(team.author, team.forum)["post"]

    with team.member.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        res = team.author.patch(
            f"/posts/{post['id']}", json={"title": "  Better  ", "tag_ids": [tag["id"]]})
        updated = recv(ws, "forum_post_updated")

    assert res.status_code == 200, res.text
    assert (res.json()["title"], res.json()["tag_ids"]) == ("Better", [tag["id"]])
    assert updated["post"] == res.json()
    assert get_post(team.owner, team.forum, post["id"])["title"] == "Better"


def test_post_edit_validation(team):
    tags = [create_tag(team.owner, team.forum, f"t{i}") for i in range(6)]
    foreign = create_tag(team.owner, team.other_forum, "elsewhere")
    post = make_post(team.author, team.forum)["post"]

    def patch(**body):
        return team.author.patch(f"/posts/{post['id']}", json=body).status_code

    assert patch(title="") == 422
    assert patch(title=None) == 422
    assert patch(tag_ids=[t["id"] for t in tags]) == 422
    assert patch(tag_ids=[foreign["id"]]) == 422
    assert patch(tag_ids=[tags[0]["id"], tags[0]["id"]]) == 200
    assert get_post(team.owner, team.forum, post["id"])["tag_ids"] == [tags[0]["id"]]
    assert team.author.patch("/posts/99999", json={"title": "x"}).status_code == 404


@pytest.mark.parametrize("body", [
    {"title": "hijacked"}, {"tag_ids": []}, {"pinned": True}, {"locked": True}])
def test_plain_members_cannot_edit_others_posts(team, body):
    post = make_post(team.author, team.forum)["post"]

    assert team.member.patch(f"/posts/{post['id']}", json=body).status_code == 403
    assert get_post(team.owner, team.forum, post["id"])["title"] == "First post"


@pytest.mark.parametrize("body", [{"pinned": True}, {"locked": True}])
def test_authors_cannot_pin_or_lock_their_own_posts(team, body):
    post = make_post(team.author, team.forum)["post"]

    assert team.author.patch(f"/posts/{post['id']}", json=body).status_code == 403


def test_moderators_can_edit_pin_lock_and_delete_any_post(team):
    tag = create_tag(team.owner, team.forum, "t")
    post = make_post(team.author, team.forum)["post"]

    res = team.mod.patch(f"/posts/{post['id']}", json={
        "title": "Moderated", "tag_ids": [tag["id"]], "pinned": True, "locked": True})

    assert res.status_code == 200, res.text
    assert (res.json()["title"], res.json()["pinned"], res.json()["locked"]) == (
        "Moderated", True, True)
    assert team.mod.delete(f"/posts/{post['id']}").status_code == 204
    assert index(team.owner, team.forum)["posts"] == []


def test_only_the_author_or_a_moderator_deletes_a_post(team):
    post = make_post(team.author, team.forum)["post"]
    other = make_post(team.author, team.forum, title="other")["post"]

    assert team.member.delete(f"/posts/{post['id']}").status_code == 403
    assert team.author.delete(f"/posts/{post['id']}").status_code == 204
    assert team.owner.delete(f"/posts/{other['id']}").status_code == 204
    assert team.owner.delete(f"/posts/{other['id']}").status_code == 404


# -- locking ---------------------------------------------------------------

def test_locked_posts_reject_replies_from_plain_members(team):
    post = make_post(team.author, team.forum)["post"]
    assert team.mod.patch(f"/posts/{post['id']}", json={"locked": True}).status_code == 200
    before = stored_messages(team.owner)

    frame = reply_frame(team, post["id"])
    assert rejected(team.member, frame) == {
        "type": "error", "code": "post_locked", "ref": frame["id"]}
    assert rejected(team.author, reply_frame(team, post["id"]))["code"] == "post_locked"
    assert stored_messages(team.owner) == before
    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 0

    reply(team.mod, team, post["id"], "moderator reply")
    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 1
    assert team.mod.patch(f"/posts/{post['id']}", json={"locked": False}).status_code == 200
    reply(team.member, team, post["id"], "open again")
    assert get_post(team.owner, team.forum, post["id"])["reply_count"] == 2


# -- deleting --------------------------------------------------------------

def test_post_delete_removes_messages_attachments_and_files(team, attachments_dir):
    reply_attachment = upload(team.member, team.forum)
    attachment = upload(team.author, team.forum)
    body = make_post(team.author, team.forum, message=opening(attachment_ids=[attachment["id"]]))
    post = body["post"]
    reply(team.member, team, post["id"], attachment_ids=[reply_attachment["id"]])
    assert team.owner.get(attachment["url"]).status_code == 200
    assert len([p for p in attachments_dir.rglob("*") if p.is_file()]) == 2

    with team.member.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert team.author.delete(f"/posts/{post['id']}").status_code == 204
        frame = recv(ws, "forum_post_deleted")

    assert frame == {"type": "forum_post_deleted", "server_id": team.sid,
                     "channel_id": team.forum, "post_id": post["id"]}
    assert team.owner.get(attachment["url"]).status_code == 404
    assert team.owner.get(reply_attachment["url"]).status_code == 404
    assert [p for p in attachments_dir.rglob("*") if p.is_file()] == []
    assert stored_messages(team.owner) == 0
    assert team.owner.get(f"/channels/{team.forum}/posts/{post['id']}").status_code == 404
    assert index(team.owner, team.forum)["posts"] == []


def test_deleting_a_post_leaves_other_posts_alone(team):
    keep = make_post(team.author, team.forum, title="keep")["post"]
    gone = make_post(team.author, team.forum, title="gone")["post"]
    reply(team.member, team, keep["id"])

    assert team.author.delete(f"/posts/{gone['id']}").status_code == 204

    assert [p["id"] for p in index(team.owner, team.forum)["posts"]] == [keep["id"]]
    assert len(post_history(team.owner, team.forum, keep["id"])["messages"]) == 2


def test_deleting_a_forum_channel_removes_its_posts(team):
    post = make_post(team.author, team.forum)["post"]
    reply(team.member, team, post["id"])

    assert team.owner.delete(f"/channels/{team.forum}").status_code == 204

    assert stored_messages(team.owner) == 0
    assert team.owner.delete(f"/posts/{post['id']}").status_code == 404


# -- channels and categories -----------------------------------------------

def test_forum_channels_sit_in_categories(team):
    group = team.owner.post(
        f"/servers/{team.sid}/channel-groups", json={"name": "Community"}).json()

    res = team.owner.post(
        f"/channels/{team.sid}/create",
        json={"name": "feedback", "type": "forum", "group_id": group["id"]})

    assert res.status_code == 201, res.text
    assert (res.json()["type"], res.json()["group_id"]) == ("forum", group["id"])
    snapshot = team.member.get(f"/servers/{team.sid}/channels").json()
    channel = next(c for c in snapshot["channels"] if c["id"] == res.json()["id"])
    assert channel["group_id"] == group["id"]
    assert team.owner.post(
        f"/channels/{team.sid}/create", json={"name": "x", "type": "thread"}).status_code == 422


def test_forum_activity_moves_the_channel_unread_marker(team):
    post = make_post(team.author, team.forum)
    msg_id = reply(team.member, team, post["post"]["id"])

    channels = team.owner.get(f"/channels/{team.sid}").json()
    forum_channel = next(c for c in channels if c["id"] == team.forum)

    assert forum_channel["last_message_id"] == msg_id
