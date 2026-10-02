"""Invite preview: readable without a session so link unfurlers can use it."""
from datetime import datetime, timedelta, timezone

from tests.conftest import create_server, register
from tests.test_realtime_events import invite_and_join


def make_invite(client, server_id, **body):
    res = client.post("/invites/", json={"server_id": server_id, **body})
    assert res.status_code == 201, res.text
    return res.json()


def test_anonymous_can_preview_valid_invite(client, new_client):
    register(client, "alice-prev")
    server = create_server(client, "Gaming")
    invite = make_invite(client, server["id"])

    res = new_client().get(f"/invites/{invite['id']}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["server_name"] == "Gaming"
    assert body["server_icon"] is None
    assert body["member_count"] == 1
    assert body["is_valid"] is True


def test_anonymous_gets_404_for_dead_invites(client, new_client):
    register(client, "alice-dead")
    server = create_server(client, "Secret place")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expired = make_invite(client, server["id"], valid_until=past)
    revoked = make_invite(client, server["id"])
    client.put(f"/invites/{revoked['id']}", json={"is_active": False})
    used_up = make_invite(client, server["id"], max_uses=1)
    joiner = _registered(new_client, "joiner-dead")
    assert joiner.post(f"/invites/{used_up['id']}/use", json={}).status_code == 200

    anonymous = new_client()
    for invite in (expired, revoked, used_up):
        res = anonymous.get(f"/invites/{invite['id']}")
        assert res.status_code == 404, res.text
        assert "Secret place" not in res.text


def test_anonymous_gets_404_for_unknown_invite(client, new_client):
    res = new_client().get("/invites/00000000-0000-0000-0000-000000000000")
    assert res.status_code == 404


def test_logged_in_user_still_sees_invalid_invite(client, new_client):
    register(client, "alice-valid")
    server = create_server(client)
    revoked = make_invite(client, server["id"])
    client.put(f"/invites/{revoked['id']}", json={"is_active": False})

    res = client.get(f"/invites/{revoked['id']}")

    assert res.status_code == 200
    assert res.json()["is_valid"] is False


def test_member_count_includes_joined_members(client, new_client):
    register(client, "alice-count")
    server = create_server(client)
    invite = make_invite(client, server["id"])
    for name in ("bob-count", "carol-count"):
        invite_and_join(client, _registered(new_client, name), server["id"])

    res = new_client().get(f"/invites/{invite['id']}")

    assert res.json()["member_count"] == 3


def _registered(new_client, username):
    c = new_client()
    register(c, username)
    return c
