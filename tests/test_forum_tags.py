"""Forum tags: limits, uniqueness, order and permissions."""
import pytest

from app.services import forum as forum_service
from tests.test_forum import (  # noqa: F401
    HEADERS, create_tag, get_post, make_post, new_post, recv, team)
from tests.conftest import ws_ready


def tags_of(client, channel_id):
    res = client.get(f"/channels/{channel_id}/tags")
    assert res.status_code == 200, res.text
    return res.json()


def test_tag_crud_and_the_shape(team):
    tag = create_tag(team.owner, team.forum, " Bug ", "#FF0000")

    assert tag == {
        "id": tag["id"], "channel_id": team.forum, "name": "Bug",
        "color": "#ff0000", "position": 0}
    assert create_tag(team.owner, team.forum, "Idea")["position"] == 1
    assert create_tag(team.owner, team.forum, "Plain")["color"] is None
    assert [t["name"] for t in tags_of(team.member, team.forum)] == ["Bug", "Idea", "Plain"]

    renamed = team.mod.patch(
        f"/channels/{team.forum}/tags/{tag['id']}", json={"name": "Defect", "color": None})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["color"]) == ("Defect", None)
    assert team.mod.delete(f"/channels/{team.forum}/tags/{tag['id']}").status_code == 204
    assert [t["name"] for t in tags_of(team.member, team.forum)] == ["Idea", "Plain"]
    assert team.mod.delete(f"/channels/{team.forum}/tags/{tag['id']}").status_code == 404


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 31},
    {"name": "ok", "color": "red"}, {"name": "ok", "color": "#12345"}, {}])
def test_tag_create_validation(team, body):
    assert team.owner.post(f"/channels/{team.forum}/tags", json=body).status_code == 422
    assert tags_of(team.owner, team.forum) == []


def test_tag_names_are_unique_per_channel_ignoring_case(team):
    tag = create_tag(team.owner, team.forum, "Bug")
    other = create_tag(team.owner, team.forum, "Idea")

    assert team.owner.post(
        f"/channels/{team.forum}/tags", json={"name": "bug"}).status_code == 409
    assert team.owner.patch(
        f"/channels/{team.forum}/tags/{other['id']}", json={"name": "BUG"}).status_code == 409
    assert team.owner.patch(
        f"/channels/{team.forum}/tags/{tag['id']}", json={"name": "BUG"}).status_code == 200
    create_tag(team.owner, team.other_forum, "bug")
    assert team.owner.patch(
        f"/channels/{team.forum}/tags/{tag['id']}", json={"name": None}).status_code == 422


def test_a_channel_has_at_most_twenty_tags(team):
    for i in range(forum_service.MAX_TAGS_PER_CHANNEL):
        create_tag(team.owner, team.forum, f"t{i}")

    assert team.owner.post(
        f"/channels/{team.forum}/tags", json={"name": "one more"}).status_code == 422
    assert len(tags_of(team.owner, team.forum)) == 20
    create_tag(team.owner, team.other_forum, "fresh channel")


def test_a_post_has_at_most_five_tags(team):
    tags = [create_tag(team.owner, team.forum, f"t{i}") for i in range(6)]
    ids = [t["id"] for t in tags]

    assert new_post(team.author, team.forum, tag_ids=ids).status_code == 422
    post = make_post(team.author, team.forum, tag_ids=ids[:5])["post"]
    assert post["tag_ids"] == ids[:5]


def test_reorder_needs_exactly_the_channels_tags(team):
    a, b, c = (create_tag(team.owner, team.forum, n) for n in "abc")
    foreign = create_tag(team.owner, team.other_forum, "x")
    url = f"/channels/{team.forum}/tags/order"

    for bad in ([a["id"], b["id"]], [a["id"], b["id"], c["id"], foreign["id"]],
                [a["id"], a["id"], b["id"], c["id"]], [a["id"], b["id"], 999], []):
        assert team.owner.put(url, json={"tag_ids": bad}).status_code == 422, bad
    assert [t["name"] for t in tags_of(team.owner, team.forum)] == ["a", "b", "c"]

    res = team.owner.put(url, json={"tag_ids": [c["id"], a["id"], b["id"]]})
    assert res.status_code == 200, res.text
    assert [t["name"] for t in res.json()] == ["c", "a", "b"]
    assert [t["position"] for t in res.json()] == [0, 1, 2]
    assert [t["name"] for t in tags_of(team.member, team.forum)] == ["c", "a", "b"]
    assert create_tag(team.owner, team.forum, "d")["position"] == 3


def test_post_tag_ids_follow_the_tag_order(team):
    a, b = create_tag(team.owner, team.forum, "a"), create_tag(team.owner, team.forum, "b")
    post = make_post(team.author, team.forum, tag_ids=[a["id"], b["id"]])["post"]

    team.owner.put(f"/channels/{team.forum}/tags/order", json={"tag_ids": [b["id"], a["id"]]})

    assert get_post(team.member, team.forum, post["id"])["tag_ids"] == [b["id"], a["id"]]


def test_deleting_a_tag_removes_it_from_posts(team):
    a, b = create_tag(team.owner, team.forum, "a"), create_tag(team.owner, team.forum, "b")
    post = make_post(team.author, team.forum, tag_ids=[a["id"], b["id"]])["post"]

    team.owner.delete(f"/channels/{team.forum}/tags/{a['id']}")

    assert get_post(team.member, team.forum, post["id"])["tag_ids"] == [b["id"]]


def test_tag_changes_broadcast_the_full_list(team):
    with team.member.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        a = create_tag(team.owner, team.forum, "a")
        created = recv(ws, "forum_tags_updated")
        b = create_tag(team.owner, team.forum, "b")
        recv(ws, "forum_tags_updated")
        team.owner.put(f"/channels/{team.forum}/tags/order", json={"tag_ids": [b["id"], a["id"]]})
        reordered = recv(ws, "forum_tags_updated")
        team.owner.patch(f"/channels/{team.forum}/tags/{a['id']}", json={"name": "A2"})
        renamed = recv(ws, "forum_tags_updated")
        team.owner.delete(f"/channels/{team.forum}/tags/{b['id']}")
        deleted = recv(ws, "forum_tags_updated")

    assert created == {
        "type": "forum_tags_updated", "server_id": team.sid, "channel_id": team.forum,
        "tags": [a]}
    assert [t["name"] for t in reordered["tags"]] == ["b", "a"]
    assert [t["name"] for t in renamed["tags"]] == ["b", "A2"]
    assert [t["name"] for t in deleted["tags"]] == ["A2"]


def test_only_channel_managers_change_tags(team):
    tag = create_tag(team.owner, team.forum, "a")
    url = f"/channels/{team.forum}/tags"

    for client in (team.member, team.author):
        assert client.post(url, json={"name": "x"}).status_code == 403
        assert client.patch(f"{url}/{tag['id']}", json={"name": "x"}).status_code == 403
        assert client.delete(f"{url}/{tag['id']}").status_code == 403
        assert client.put(f"{url}/order", json={"tag_ids": [tag["id"]]}).status_code == 403
        assert client.get(url).status_code == 200
    assert team.mod.post(url, json={"name": "by-mod"}).status_code == 201
    assert [t["name"] for t in tags_of(team.owner, team.forum)] == ["a", "by-mod"]


def test_tag_endpoints_reject_text_channels_and_foreign_tags(team):
    tag = create_tag(team.owner, team.forum, "a")

    assert team.owner.get(f"/channels/{team.text}/tags").status_code == 422
    assert team.owner.post(f"/channels/{team.text}/tags", json={"name": "x"}).status_code == 422
    assert team.owner.patch(
        f"/channels/{team.other_forum}/tags/{tag['id']}", json={"name": "x"}).status_code == 404
    assert team.owner.delete(
        f"/channels/{team.other_forum}/tags/{tag['id']}").status_code == 404
