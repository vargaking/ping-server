from datetime import datetime, timedelta, timezone

from app.models.Token import Token
from tests.conftest import PASSWORD, register


def test_register_sets_cookie_and_me_works(client):
    me = register(client, "alice")
    assert me["username"] == "alice"
    assert "password_hash" not in me
    assert client.cookies.get("access_token")


def test_me_requires_auth(client):
    assert client.get("/auth/me").status_code == 401


def test_duplicate_username_rejected(client, new_client):
    register(client, "alice")
    res = new_client().post("/auth/register", json={"username": "alice", "password": PASSWORD})
    assert res.status_code == 400


def test_login_wrong_password(client, new_client):
    register(client, "alice")
    res = new_client().post("/auth/login", json={"username": "alice", "password": "nope"})
    assert res.status_code == 401


def test_login_ok(client, new_client):
    register(client, "alice")
    other = new_client()
    res = other.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert res.status_code == 200
    assert other.get("/auth/me").json()["username"] == "alice"


def test_garbage_cookie_is_anonymous(client):
    client.cookies.set("access_token", "not-a-real-token")
    assert client.get("/auth/me").status_code == 401


# --- ZET-7: token expiry + logout -----------------------------------------

def test_login_sets_expires_at(client, new_client):
    register(client, "alice")
    other = new_client()
    assert other.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    token = other.cookies.get("access_token")

    async def _get():
        return await Token.get_or_none(token=token)

    obj = client.portal.call(_get)
    assert obj is not None
    assert obj.expires_at is not None
    assert obj.expires_at > datetime.now(timezone.utc)


def test_expired_token_is_rejected_and_deleted(client, new_client):
    me = register(client, "alice")

    async def _make():
        await Token.create(
            user_id=me["id"],
            token="expired-tok",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

    client.portal.call(_make)

    other = new_client()
    other.cookies.set("access_token", "expired-tok")
    assert other.get("/auth/me").status_code == 401

    async def _exists():
        return await Token.exists(token="expired-tok")

    # the expired row is cleaned up as a side effect of the rejected request
    assert client.portal.call(_exists) is False


def test_logout_invalidates_session(client):
    register(client, "alice")
    assert client.get("/auth/me").status_code == 200
    token = client.cookies.get("access_token")

    assert client.post("/auth/logout").status_code == 200

    # cookie cleared client-side...
    assert client.get("/auth/me").status_code == 401

    # ...and the token row is gone server-side
    async def _exists():
        return await Token.exists(token=token)

    assert client.portal.call(_exists) is False


# --- ZET-11: auth rate limiting -------------------------------------------

def test_login_rate_limit_returns_429_with_retry_after(client, new_client, monkeypatch):
    register(client, "throttled")

    # Lower the limit just for this test; env is read per request and restored
    # by monkeypatch afterwards. A distinct limit string is its own counter.
    monkeypatch.setenv("AUTH_RATE_LIMIT", "3/minute")

    other = new_client()
    creds = {"username": "throttled", "password": PASSWORD}
    for _ in range(3):
        assert other.post("/auth/login", json=creds).status_code == 200

    limited = other.post("/auth/login", json=creds)
    assert limited.status_code == 429
    assert any(k.lower() == "retry-after" for k in limited.headers)
