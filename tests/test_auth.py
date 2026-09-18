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
