"""GET /admin/errors: recent client and server errors, grouped."""
import pytest
from fastapi import HTTPException

from app.app import app
from app.services import recent_errors
from tests.conftest import register
from tests.test_server_requests import make_admin

URL = "/admin/errors"
REPORT = {
    "kind": "error",
    "message": "boom",
    "stack": "at foo (app.js:1:2)",
    "url": "https://x.test/chat/1?token=secret#top",
}


@pytest.fixture(autouse=True)
def clear_buffers():
    recent_errors.client.clear()
    recent_errors.server.clear()
    yield
    recent_errors.client.clear()
    recent_errors.server.clear()


@pytest.fixture
def failing_routes():
    async def boom():
        raise RuntimeError("kaboom")

    async def unavailable():
        raise HTTPException(status_code=503, detail="down")

    app.add_api_route("/test-boom", boom, methods=["GET"])
    app.add_api_route("/test-unavailable", unavailable, methods=["GET"])
    yield
    app.router.routes.pop()
    app.router.routes.pop()


def groups(client, **params) -> list[dict]:
    res = client.get(URL, params=params)
    assert res.status_code == 200, res.text
    return res.json()["groups"]


def test_client_report_is_listed_with_path_only(client):
    me = make_admin(client, "root")
    assert client.post("/api/client-errors", json=REPORT).status_code == 204

    body = client.get(URL).json()

    assert body["limit"] == recent_errors.MAX_ENTRIES
    assert body["since"]
    (group,) = body["groups"]
    assert group["source"] == "client"
    assert group["kind"] == "error"
    assert group["message"] == "boom"
    assert group["stack"] == "at foo (app.js:1:2)"
    assert group["path"] == "/chat/1"
    assert group["user_id"] == me["id"]
    assert group["count"] == 1


def test_identical_reports_are_grouped(client):
    make_admin(client, "root")
    for _ in range(3):
        client.post("/api/client-errors", json=REPORT)

    (group,) = groups(client)

    assert group["count"] == 3
    assert group["first_at"] <= group["last_at"]


def test_server_exception_is_listed_with_request_id_and_traceback(client, failing_routes):
    make_admin(client, "root")
    res = client.get("/test-boom")
    assert res.status_code == 500

    (group,) = groups(client)

    assert group["source"] == "server"
    assert group["kind"] == "RuntimeError"
    assert group["message"] == "kaboom"
    assert group["method"] == "GET"
    assert group["path"] == "/test-boom"
    assert group["request_id"] == res.headers["x-request-id"]
    assert "Traceback" in group["stack"] and "RuntimeError: kaboom" in group["stack"]


def test_http_5xx_is_listed_without_stack(client, failing_routes):
    make_admin(client, "root")
    client.get("/test-unavailable")

    (group,) = groups(client)

    assert group["kind"] == "http_503"
    assert group["message"] == "HTTP 503"
    assert group["path"] == "/test-unavailable"
    assert group["stack"] is None


def test_source_filter(client, failing_routes):
    make_admin(client, "root")
    client.post("/api/client-errors", json=REPORT)
    client.get("/test-boom")

    assert {g["source"] for g in groups(client)} == {"client", "server"}
    assert {g["source"] for g in groups(client, source="client")} == {"client"}
    assert {g["source"] for g in groups(client, source="server")} == {"server"}
    assert client.get(URL, params={"source": "other"}).status_code == 422


def test_buffer_is_bounded(monkeypatch):
    monkeypatch.setattr(recent_errors, "MAX_ENTRIES", 3)
    buffer = recent_errors.RecentErrors()
    for i in range(5):
        buffer.record(source="client", kind="error", message=f"m{i}")

    assert [e.message for e in buffer.entries()] == ["m2", "m3", "m4"]


def test_long_text_is_truncated():
    buffer = recent_errors.RecentErrors()
    long = "a" * 10_000 + "END"
    buffer.record(source="server", kind="x", message=long, stack=long)
    buffer.record(source="client", kind="x", message="m", stack=long)

    server_entry, client_entry = buffer.entries()
    assert len(server_entry.message) == recent_errors.MAX_MESSAGE_CHARS
    assert server_entry.stack.endswith("END")
    assert len(server_entry.stack) == recent_errors.MAX_STACK_CHARS
    assert client_entry.stack.startswith("a")
    assert len(client_entry.stack) == recent_errors.MAX_STACK_CHARS


def test_non_admin_is_forbidden(client):
    register(client, "regular")
    assert client.get(URL).status_code == 403


def test_anonymous_is_unauthorized(client):
    assert client.get(URL).status_code == 401
