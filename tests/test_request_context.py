import logging

import pytest

from app.app import app
from tests.conftest import ORIGIN, register


@pytest.fixture
def boom_route():
    async def boom():
        raise RuntimeError("kaboom")

    app.add_api_route("/test-boom", boom, methods=["GET"])
    yield "/test-boom"
    app.router.routes.pop()


def test_unhandled_error_returns_json_500_with_cors_and_request_id(client, boom_route, caplog):
    me = register(client)

    with caplog.at_level(logging.ERROR, logger="app.errors"):
        res = client.get(boom_route, headers={"Origin": ORIGIN, "X-Request-ID": "abc-123"})

    assert res.status_code == 500
    assert res.json() == {"detail": "Internal server error", "request_id": "abc-123"}
    assert res.headers["x-request-id"] == "abc-123"
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert "x-request-id" in res.headers["access-control-expose-headers"].lower()

    records = [r for r in caplog.records if r.name == "app.errors"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    message = records[0].getMessage()
    assert f"user_id={me['id']}" in message
    assert "request_id=abc-123" in message
    assert "GET /test-boom" in message


def test_request_id_is_generated_when_missing_or_invalid(client, boom_route):
    res = client.get(boom_route, headers={"X-Request-ID": "bad id!\n"})
    rid = res.json()["request_id"]
    assert len(rid) == 12 and rid.isalnum()
    assert res.headers["x-request-id"] == rid

    ok = client.get("/")
    assert ok.status_code == 200
    assert len(ok.headers["x-request-id"]) == 12


def test_anonymous_error_logs_dash_for_user(client, boom_route, caplog):
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        client.get(boom_route)
    assert "user_id=-" in caplog.records[-1].getMessage()
