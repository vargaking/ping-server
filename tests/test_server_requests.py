"""Server creation waitlist: SERVER_CREATION=waitlist gates POST /servers behind
a request that a platform admin approves or declines."""
import pytest

from app.models.ServerRequest import ServerRequest
from app.models.User import User
from app.scripts.platform_admin import set_platform_admin
from tests.conftest import ORIGIN, create_server, register, ws_ready
from tests.test_push import AUTH, FCM, P256DH, push_on, vapid_env  # noqa: F401

HEADERS = {"origin": ORIGIN}
BODY = {"name": "Chess club", "description": "A place for weekly games.", "expected_size": "lt10"}


@pytest.fixture
def waitlist(monkeypatch):
    monkeypatch.setenv("SERVER_CREATION", "waitlist")


def make_admin(client, username):
    me = register(client, username)

    async def grant():
        await User.filter(id=me["id"]).update(is_platform_admin=True)
    client.portal.call(grant)
    return me


def submit(client, **overrides):
    return client.post("/server-requests", json={**BODY, **overrides})


def test_open_mode_lets_anyone_create_and_nothing_to_request(client):
    register(client, "open-user")
    create_server(client)
    state = client.get("/server-requests/me").json()
    assert state == {"mode": "open", "can_create": True, "request": None}
    assert submit(client).status_code == 409


def test_waitlist_blocks_server_creation_for_users_but_not_admins(client, new_client, waitlist):
    register(client, "wl-user")
    res = client.post("/servers/", json={"name": "Nope"})
    assert res.status_code == 403
    assert client.get("/server-requests/me").json()["can_create"] is False

    admin_client = new_client()
    make_admin(admin_client, "wl-admin")
    assert admin_client.post("/servers/", json={"name": "Fine"}).status_code == 201
    assert admin_client.get("/server-requests/me").json()["can_create"] is True


@pytest.mark.parametrize("overrides", [
    {"description": ""},
    {"description": "   "},
    {"description": "x" * 501},
    {"expected_size": "huge"},
    {"name": "  "},
    {"name": "n" * 101},
])
def test_invalid_requests_are_rejected(client, waitlist, overrides):
    register(client, "invalid-user")
    assert submit(client, **overrides).status_code == 422


def test_create_request_is_pending_and_shows_in_me(client, waitlist):
    register(client, "pending-user")
    res = submit(client, name="  Chess club  ", description="  games  ")
    assert res.status_code == 201
    body = res.json()
    assert (body["name"], body["description"]) == ("Chess club", "games")
    assert body["status"] == "pending"
    assert client.get("/server-requests/me").json()["request"]["id"] == body["id"]


def test_second_request_while_pending_conflicts(client, waitlist):
    register(client, "twice-user")
    assert submit(client).status_code == 201
    res = submit(client)
    assert res.status_code == 409
    assert res.json()["detail"] == "You already have a pending request"


def test_withdraw(client, new_client, waitlist):
    register(client, "withdraw-user")
    submit(client)
    admin_client = new_client()
    make_admin(admin_client, "withdraw-admin")
    assert len(admin_client.get("/server-requests").json()) == 1

    assert client.delete("/server-requests/me").status_code == 204
    assert admin_client.get("/server-requests").json() == []
    assert client.get("/server-requests/me").json()["request"] is None
    assert client.delete("/server-requests/me").status_code == 404
    assert submit(client).status_code == 201


def test_admin_endpoints_need_a_platform_admin(client, new_client, waitlist):
    register(client, "plain-user")
    request_id = submit(client).json()["id"]
    assert client.get("/server-requests").status_code == 403
    assert client.post(f"/server-requests/{request_id}/approve").status_code == 403
    assert client.post(f"/server-requests/{request_id}/decline", json={}).status_code == 403
    assert new_client().get("/server-requests").status_code == 401


def test_admin_list_shows_only_the_requester_name(client, new_client, waitlist):
    me = register(client, "listed-user")
    submit(client)
    admin_client = new_client()
    make_admin(admin_client, "list-admin")
    [row] = admin_client.get("/server-requests").json()
    assert row["requester"] == {"id": me["id"], "username": "listed-user"}
    assert row["description"] == BODY["description"]
    assert admin_client.get("/server-requests?status=all").status_code == 200
    assert admin_client.get("/server-requests?status=bogus").status_code == 422


def test_approve_creates_the_server_for_the_requester(client, new_client, waitlist):
    me = register(client, "approved-user")
    request_id = submit(client).json()["id"]
    admin_client = new_client()
    make_admin(admin_client, "approve-admin")

    res = admin_client.post(f"/server-requests/{request_id}/approve")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "approved"
    server_id = body["server_id"]

    [server] = client.get("/servers/me").json()
    assert server["id"] == server_id
    assert server["name"] == "Chess club"
    assert server["owner_id"] == me["id"]
    assert [m["id"] for m in server["members"]] == [me["id"]]
    assert server["permissions"] != "0"
    roles = client.get(f"/servers/{server_id}/roles").json()
    assert {r["name"] for r in roles} >= {"@everyone"}

    assert client.get("/server-requests/me").json()["request"]["status"] == "approved"
    assert admin_client.post(f"/server-requests/{request_id}/approve").status_code == 409
    assert admin_client.post(f"/server-requests/{request_id}/decline", json={}).status_code == 409
    assert admin_client.post("/server-requests/9999/approve").status_code == 404


def test_approve_tells_the_requester_live(client, new_client, waitlist):
    register(client, "live-user")
    request_id = submit(client).json()["id"]
    admin_client = new_client()
    make_admin(admin_client, "live-admin")

    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        admin_client.post(f"/server-requests/{request_id}/approve")
        updated = ws.receive_json()
        added = ws.receive_json()

    assert updated["type"] == "server_request_updated"
    assert updated["request"]["status"] == "approved"
    assert added["type"] == "server_added"
    assert added["server"]["id"] == updated["request"]["server_id"]
    assert added["server"]["permissions"] != "0"


def test_approve_pushes_when_the_requester_has_no_active_session(
        client, new_client, waitlist, push_on):  # noqa: F811
    register(client, "push-user")
    endpoint = f"{FCM}push-user"
    sub = client.post("/api/push/subscriptions", json={
        "endpoint": endpoint, "keys": {"p256dh": P256DH, "auth": AUTH}})
    assert sub.status_code in (200, 201), sub.text
    request_id = submit(client).json()["id"]
    admin_client = new_client()
    make_admin(admin_client, "push-admin")

    res = admin_client.post(f"/server-requests/{request_id}/approve")
    from app.services.push import push
    client.portal.call(push.drain)

    [call] = push_on.calls
    server_id = res.json()["server_id"]
    assert call["payload"]["kind"] == "server_request"
    assert call["payload"]["body"] == "Chess club"
    assert call["payload"]["url"] == f"/app/server/{server_id}/"
    assert call["payload"]["tag"] == f"server-request-{request_id}"


def test_decline_keeps_the_reason_and_allows_a_new_request(client, new_client, waitlist):
    register(client, "declined-user")
    request_id = submit(client).json()["id"]
    admin_client = new_client()
    make_admin(admin_client, "decline-admin")

    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        res = admin_client.post(
            f"/server-requests/{request_id}/decline", json={"reason": "  Too vague  "})
        assert res.status_code == 200
        frame = ws.receive_json()
    assert frame["type"] == "server_request_updated"
    assert frame["request"]["status"] == "declined"

    mine = client.get("/server-requests/me").json()["request"]
    assert (mine["status"], mine["decline_reason"]) == ("declined", "Too vague")
    assert client.get("/servers/me").json() == []
    assert submit(client).status_code == 201


def test_decline_reason_is_optional_and_bounded(client, new_client, waitlist):
    register(client, "reason-user")
    request_id = submit(client).json()["id"]
    admin_client = new_client()
    make_admin(admin_client, "reason-admin")
    too_long = admin_client.post(
        f"/server-requests/{request_id}/decline", json={"reason": "x" * 501})
    assert too_long.status_code == 422
    res = admin_client.post(f"/server-requests/{request_id}/decline")
    assert res.status_code == 200
    assert res.json()["decline_reason"] is None


def test_history_lists_own_requests_newest_first_including_withdrawn(
        client, new_client, waitlist):
    register(client, "history-user")
    admin_client = new_client()
    make_admin(admin_client, "history-admin")

    declined_id = submit(client, name="First").json()["id"]
    admin_client.post(f"/server-requests/{declined_id}/decline", json={"reason": "Too vague"})
    withdrawn_id = submit(client, name="Second").json()["id"]
    client.delete("/server-requests/me")
    pending_id = submit(client, name="Third").json()["id"]

    res = client.get("/server-requests/me/history")
    assert res.status_code == 200
    rows = res.json()
    assert [r["id"] for r in rows] == [pending_id, withdrawn_id, declined_id]
    assert [r["status"] for r in rows] == ["pending", "withdrawn", "declined"]
    assert rows[2]["decline_reason"] == "Too vague"
    assert rows[2]["decided_at"] is not None
    assert set(rows[0]) == {
        "id", "name", "description", "expected_size", "status",
        "decline_reason", "created_at", "decided_at", "server_id"}


def test_history_works_in_open_mode(client):
    register(client, "history-open-user")
    assert client.get("/server-requests/me/history").json() == []


def test_history_only_has_the_callers_requests(client, new_client, waitlist):
    register(client, "history-mine")
    mine = submit(client, name="Mine").json()["id"]
    other_client = new_client()
    register(other_client, "history-other")
    theirs = submit(other_client, name="Theirs").json()["id"]

    assert [r["id"] for r in client.get("/server-requests/me/history").json()] == [mine]
    assert [r["id"] for r in other_client.get("/server-requests/me/history").json()] == [theirs]


def test_history_is_empty_for_a_user_without_requests(client, waitlist):
    register(client, "history-empty")
    assert client.get("/server-requests/me/history").json() == []


def test_history_is_capped_at_50_and_drops_the_oldest(client, waitlist):
    me = register(client, "history-capped")

    async def seed():
        return [
            (await ServerRequest.create(
                user_id=me["id"], name=f"Club {i}", description="d",
                expected_size="lt10", status="withdrawn")).id
            for i in range(51)
        ]
    ids = client.portal.call(seed)

    rows = client.get("/server-requests/me/history").json()
    assert len(rows) == 50
    assert [r["id"] for r in rows] == ids[:0:-1]
    assert ids[0] not in {r["id"] for r in rows}


def test_history_needs_authentication(new_client):
    assert new_client().get("/server-requests/me/history").status_code == 401


def test_me_includes_platform_admin_flag_only_for_the_caller(client, new_client):
    register(client, "flag-user")
    assert client.get("/auth/me").json()["is_platform_admin"] is False
    admin_client = new_client()
    me = make_admin(admin_client, "flag-admin")
    assert admin_client.get("/auth/me").json()["is_platform_admin"] is True
    assert "is_platform_admin" not in admin_client.get("/users/").json()[0]
    assert me["id"]


def test_cli_grants_and_revokes(client):
    me = register(client, "cli-user")

    def flag():
        return client.portal.call(
            lambda: User.get(id=me["id"])).is_platform_admin

    assert flag() is False
    assert client.portal.call(set_platform_admin, "cli-user", True) is True
    assert flag() is True
    assert client.portal.call(set_platform_admin, "cli-user", False) is True
    assert flag() is False
    assert client.portal.call(set_platform_admin, "nobody", True) is False
