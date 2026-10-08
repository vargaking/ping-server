"""Which address the rate limits key on, behind the Cloudflare Worker and without it."""
from types import SimpleNamespace

import pytest
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.rate_limit import client_address, invite_use_key, limiter, user_key
from tests.conftest import PASSWORD, register

KEY = "worker-secret-0123456789"
REMOTE = "203.0.113.9"


def make_request(headers: dict[str, str] | None = None, path_params=None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (REMOTE, 50000),
        "path_params": path_params or {},
    }
    return Request(scope)


HEADER_CASES = [
    {},
    {"X-Zet-Client-IP": "198.51.100.7"},
    {"X-Zet-Client-IP": "198.51.100.7", "X-Zet-Proxy-Key": KEY},
    {"X-Zet-Client-IP": "not an address", "X-Zet-Proxy-Key": "wrong"},
    {"X-Forwarded-For": "198.51.100.8"},
]


@pytest.mark.parametrize("headers", HEADER_CASES)
def test_without_the_env_var_the_key_is_exactly_what_it_was(monkeypatch, headers):
    monkeypatch.delenv("TRUSTED_PROXY_KEY", raising=False)
    request = make_request(headers)
    assert client_address(request) == get_remote_address(request) == REMOTE


@pytest.mark.parametrize("headers", HEADER_CASES)
def test_an_empty_env_var_counts_as_unset(monkeypatch, headers):
    monkeypatch.setenv("TRUSTED_PROXY_KEY", "")
    assert client_address(make_request(headers)) == REMOTE


def test_the_worker_header_is_used_with_the_right_key(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_KEY", KEY)
    request = make_request({"X-Zet-Client-IP": " 198.51.100.7 ", "X-Zet-Proxy-Key": KEY})
    assert client_address(request) == "198.51.100.7"

    v6 = make_request({"X-Zet-Client-IP": "2001:DB8::1", "X-Zet-Proxy-Key": KEY})
    assert client_address(v6) == "2001:db8::1"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Zet-Client-IP": "198.51.100.7"},
        {"X-Zet-Client-IP": "198.51.100.7", "X-Zet-Proxy-Key": "wrong"},
        {"X-Zet-Client-IP": "198.51.100.7", "X-Zet-Proxy-Key": KEY + "x"},
        {"X-Zet-Client-IP": "198.51.100.7", "X-Zet-Proxy-Key": ""},
        {"X-Zet-Client-IP": "not an address", "X-Zet-Proxy-Key": KEY},
        {"X-Zet-Client-IP": "198.51.100.7, 10.0.0.1", "X-Zet-Proxy-Key": KEY},
        {"X-Zet-Proxy-Key": KEY},
    ],
)
def test_the_header_is_ignored_without_the_key_or_a_valid_address(monkeypatch, headers):
    monkeypatch.setenv("TRUSTED_PROXY_KEY", KEY)
    assert client_address(make_request(headers)) == REMOTE


def test_invite_and_user_keys_use_the_same_address(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_KEY", KEY)
    headers = {"X-Zet-Client-IP": "198.51.100.7", "X-Zet-Proxy-Key": KEY}
    assert invite_use_key(make_request(headers, {"invite_id": "abc"})) == "198.51.100.7:abc"

    anonymous = make_request(headers)
    anonymous.state.user = None
    assert user_key(anonymous) == "198.51.100.7"

    signed_in = make_request(headers)
    signed_in.state.user = SimpleNamespace(id=5)
    assert user_key(signed_in) == "user:5"


@pytest.fixture
def fresh_limits():
    limiter.reset()
    yield
    limiter.reset()


def fail_login(client, headers):
    return client.post(
        "/auth/login", json={"username": "proxied", "password": "wrong password"}, headers=headers
    ).status_code


def test_two_addresses_through_the_worker_get_separate_budgets(client, monkeypatch, fresh_limits):
    register(client, "proxied")
    monkeypatch.setenv("TRUSTED_PROXY_KEY", KEY)
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    laptop = {"X-Zet-Client-IP": "198.51.100.1", "X-Zet-Proxy-Key": KEY}
    phone = {"X-Zet-Client-IP": "198.51.100.2", "X-Zet-Proxy-Key": KEY}

    assert [fail_login(client, laptop) for _ in range(3)] == [401, 401, 429]
    assert fail_login(client, phone) == 401
    assert client.post(
        "/auth/login", json={"username": "proxied", "password": PASSWORD}, headers=phone
    ).status_code == 200


def test_made_up_addresses_without_the_key_share_one_budget(client, monkeypatch, fresh_limits):
    register(client, "proxied")
    monkeypatch.setenv("TRUSTED_PROXY_KEY", KEY)
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    codes = [fail_login(client, {"X-Zet-Client-IP": f"198.51.100.{i}"}) for i in range(3)]
    assert codes == [401, 401, 429]


def test_without_the_env_var_the_worker_header_changes_nothing(client, monkeypatch, fresh_limits):
    register(client, "proxied")
    monkeypatch.delenv("TRUSTED_PROXY_KEY", raising=False)
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    codes = [
        fail_login(client, {"X-Zet-Client-IP": f"198.51.100.{i}", "X-Zet-Proxy-Key": KEY})
        for i in range(3)
    ]
    assert codes == [401, 401, 429]
