"""Platform announcements: admins post notices, everyone reads and dismisses them."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.Announcement import Announcement
from app.models.User import User
from tests.conftest import ORIGIN, register, ws_ready
from tests.test_permissions import run

HEADERS = {"origin": ORIGIN}
ADMIN_URL = "/api/admin/announcements"
USER_URL = "/api/announcements"
BODY = {"text": "Maintenance tonight", "level": "maintenance"}


def make_admin(client, username):
    me = register(client, username)

    async def grant():
        await User.filter(id=me["id"]).update(is_platform_admin=True)
    run(client, grant)
    return me


def future(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def seed(client, text, *, age=timedelta(0), expires_in=None):
    """Insert a row directly so expired and old ones exist without waiting."""
    async def create():
        now = datetime.now(timezone.utc)
        row = await Announcement.create(
            text=text, level="info",
            expires_at=None if expires_in is None else now + expires_in)
        await Announcement.filter(id=row.id).update(created_at=now - age)
        return row.id
    return run(client, create)


def create(admin, **overrides):
    return admin.post(ADMIN_URL, json={**BODY, **overrides})


def listed(client):
    res = client.get(USER_URL)
    assert res.status_code == 200, res.text
    return {item["text"]: item for item in res.json()}


@pytest.fixture
def admin(client):
    make_admin(client, "ann-admin")
    return client


@pytest.fixture
def member(new_client):
    other = new_client()
    register(other, "ann-member")
    return other


@pytest.mark.parametrize("method, path, body", [
    ("post", ADMIN_URL, BODY),
    ("post", f"{ADMIN_URL}/1/end", None),
    ("get", ADMIN_URL, None),
])
def test_admin_routes_refuse_non_admins_and_anonymous(client, new_client, method, path, body):
    anonymous = new_client()
    assert getattr(anonymous, method)(path, **({"json": body} if body else {})).status_code == 401

    register(client, "ann-plain")
    res = getattr(client, method)(path, **({"json": body} if body else {}))
    assert res.status_code == 403
    assert not client.get(USER_URL).json()


@pytest.mark.parametrize("method, path", [
    ("get", USER_URL),
    ("post", f"{USER_URL}/1/dismiss"),
])
def test_user_routes_need_a_session(client, method, path):
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize("overrides", [
    {"text": ""},
    {"text": "   "},
    {"text": "x" * 501},
    {"level": "urgent"},
    {"expires_at": "2020-01-01T00:00:00+00:00"},
    {"expires_at": "2999-01-01T00:00:00"},
])
def test_create_rejects_bad_input(admin, overrides):
    assert create(admin, **overrides).status_code == 422
    assert admin.get(ADMIN_URL).json() == []


def test_create_returns_the_admin_object(admin):
    me = admin.get("/auth/me").json()
    expires = future(hours=1)
    res = create(admin, text="  Heads up  ", level="info", expires_at=expires)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["text"] == "Heads up"
    assert body["level"] == "info"
    assert body["ended"] is False
    assert body["created_by"] == {"id": me["id"], "username": me["username"]}
    assert datetime.fromisoformat(body["expires_at"]) == datetime.fromisoformat(expires)
    assert "dismissed" not in body
    assert admin.get(ADMIN_URL).json() == [body]


def test_create_accepts_500_characters_without_expiry(admin):
    res = create(admin, text="x" * 500)
    assert res.status_code == 201, res.text
    assert res.json()["expires_at"] is None


def test_user_list_has_recent_ones_newest_first_with_ended_and_dismissed(admin, member):
    seed(admin, "old", age=timedelta(days=31))
    seed(admin, "expired", age=timedelta(days=3), expires_in=timedelta(days=-1))
    seed(admin, "active", age=timedelta(days=2), expires_in=timedelta(days=1))
    seed(admin, "open ended", age=timedelta(days=1))
    dismissed_id = listed(member)["open ended"]["id"]
    assert member.post(f"{USER_URL}/{dismissed_id}/dismiss").status_code == 204

    res = member.get(USER_URL).json()
    assert [item["text"] for item in res] == ["open ended", "active", "expired"]
    by_text = {item["text"]: item for item in res}
    assert {text: (item["ended"], item["dismissed"]) for text, item in by_text.items()} == {
        "open ended": (False, True),
        "active": (False, False),
        "expired": (True, False),
    }
    assert set(by_text["active"]) == {
        "id", "text", "level", "created_at", "expires_at", "ended", "dismissed"}


def test_admin_list_has_everything_newest_first(admin):
    seed(admin, "old", age=timedelta(days=31))
    seed(admin, "recent", age=timedelta(days=1))
    seed(admin, "expired", age=timedelta(days=2), expires_in=timedelta(days=-1))
    res = admin.get(ADMIN_URL).json()
    assert [(item["text"], item["ended"]) for item in res] == [
        ("recent", False), ("expired", True), ("old", False)]
    assert all(item["created_by"] is None for item in res)


def test_dismissal_is_per_user_and_idempotent(admin, member):
    announcement_id = create(admin).json()["id"]
    url = f"{USER_URL}/{announcement_id}/dismiss"
    assert member.post(url).status_code == 204
    assert member.post(url).status_code == 204

    assert listed(member)[BODY["text"]]["dismissed"] is True
    assert listed(admin)[BODY["text"]]["dismissed"] is False


def test_dismissing_an_unknown_announcement_is_404(member):
    assert member.post(f"{USER_URL}/999/dismiss").status_code == 404


def test_end_is_idempotent_and_marks_it_ended(admin):
    announcement_id = create(admin, expires_at=future(days=1)).json()["id"]
    url = f"{ADMIN_URL}/{announcement_id}/end"

    first = admin.post(url)
    assert first.status_code == 200, first.text
    assert first.json()["ended"] is True
    assert datetime.fromisoformat(first.json()["expires_at"]) <= datetime.now(timezone.utc)

    second = admin.post(url)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert listed(admin)[BODY["text"]]["ended"] is True


def test_end_keeps_an_earlier_expiry(admin):
    announcement_id = seed(admin, "gone", expires_in=timedelta(days=-1))

    async def expiry():
        return (await Announcement.get(id=announcement_id)).expires_at

    before = run(admin, expiry)
    assert admin.post(f"{ADMIN_URL}/{announcement_id}/end").status_code == 200
    assert run(admin, expiry) == before


def test_ending_an_unknown_announcement_is_404(admin):
    assert admin.post(f"{ADMIN_URL}/999/end").status_code == 404


def test_online_users_get_frames_on_create_and_end(admin, member):
    with admin.websocket_connect("/ws", headers=HEADERS) as admin_ws, \
            member.websocket_connect("/ws", headers=HEADERS) as member_ws:
        ws_ready(admin_ws)
        ws_ready(member_ws)

        created = create(admin).json()
        for ws in (member_ws, admin_ws):
            frame = ws.receive_json()
            assert frame["type"] == "announcement"
            assert frame["announcement"]["id"] == created["id"]
            assert frame["announcement"]["text"] == BODY["text"]
            assert frame["announcement"]["dismissed"] is False
            assert frame["announcement"]["ended"] is False
            assert "created_by" not in frame["announcement"]

        ended = admin.post(f"{ADMIN_URL}/{created['id']}/end").json()
        for ws in (member_ws, admin_ws):
            assert ws.receive_json() == {
                "type": "announcement_ended",
                "id": created["id"],
                "expires_at": ended["expires_at"],
            }

        # A repeated end sends nothing: the next frame is the next create.
        assert admin.post(f"{ADMIN_URL}/{created['id']}/end").status_code == 200
        following = create(admin, text="Next").json()
        for ws in (member_ws, admin_ws):
            assert ws.receive_json()["announcement"]["id"] == following["id"]


def test_offline_users_find_it_in_the_list(admin, member):
    create(admin)
    assert BODY["text"] in listed(member)
