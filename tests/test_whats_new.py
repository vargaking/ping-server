import pytest

from app.models.User import User
from tests.conftest import PASSWORD, register
from tests.test_permissions import run

URL = "/whats-new/state"
RULE = "Use YYYY-MM-DD or YYYY-MM-DD-N (N 2–9)."


def stored(client, user_id: int) -> User:
    async def load():
        return await User.get(id=user_id)

    return run(client, load)


def put(client, value):
    return client.put(URL, json={"last_seen_id": value})


def test_both_endpoints_require_a_session(client):
    assert client.get(URL).status_code == 401
    assert put(client, "2026-10-10").status_code == 401


def test_new_user_has_seen_nothing(client):
    me = register(client)
    res = client.get(URL)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["last_seen_id"] is None
    assert body["created_on"] == stored(client, me["id"]).created_at.date().isoformat()


def test_put_is_stored_and_visible_from_another_client(client, new_client):
    me = register(client, "alice")
    res = put(client, "2026-10-10")
    assert res.status_code == 200, res.text
    assert res.json()["last_seen_id"] == "2026-10-10"
    assert client.get(URL).json()["last_seen_id"] == "2026-10-10"

    other = new_client()
    assert other.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert other.get(URL).json() == client.get(URL).json()
    assert stored(client, me["id"]).whats_new_seen_id == "2026-10-10"


def test_it_only_moves_forward(client):
    me = register(client)
    assert put(client, "2026-10-10-2").json()["last_seen_id"] == "2026-10-10-2"

    res = put(client, "2026-10-10")
    assert res.status_code == 200
    assert res.json()["last_seen_id"] == "2026-10-10-2"
    assert stored(client, me["id"]).whats_new_seen_id == "2026-10-10-2"

    assert put(client, "2026-10-10-2").json()["last_seen_id"] == "2026-10-10-2"
    assert put(client, "2026-10-11").json()["last_seen_id"] == "2026-10-11"
    assert stored(client, me["id"]).whats_new_seen_id == "2026-10-11"


def test_extra_keys_are_ignored(client):
    register(client)
    res = client.put(URL, json={"last_seen_id": "2026-10-10", "other": 1})
    assert res.status_code == 200
    assert res.json()["last_seen_id"] == "2026-10-10"


@pytest.mark.parametrize("value", [
    "", "2026-13-01", "2026-02-30", "2026-10-10-1", "2026-10-10-10", "2026-10-10-a",
    " 2026-10-10", "2026-10-10 ", "2026-10-10\n", "x" * 100, 20261010, None, "٢٠٢٦-10-10",
])
def test_invalid_ids_are_refused_and_nothing_changes(client, value):
    me = register(client)
    assert put(client, "2026-01-01").status_code == 200

    res = put(client, value)
    assert res.status_code == 422
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "last_seen_id"]
    if isinstance(value, str):
        assert error["msg"] == RULE
    assert stored(client, me["id"]).whats_new_seen_id == "2026-01-01"


def test_users_do_not_share_state(client, new_client):
    register(client, "alice")
    bob = new_client()
    register(bob, "bob")
    put(client, "2026-10-10")
    assert bob.get(URL).json()["last_seen_id"] is None


def test_the_marker_is_not_exposed_elsewhere(client):
    me = register(client)
    put(client, "2026-10-10")

    payloads = [
        client.get("/auth/me").json(),
        client.get(f"/users/{me['id']}").json(),
        *client.get("/users/").json(),
    ]
    assert len(payloads) >= 2
    for payload in payloads:
        assert "whats_new_seen_id" not in payload
        assert "last_seen_id" not in payload
