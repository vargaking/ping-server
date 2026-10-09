import logging
import re

import anyio
import httpx
import pytest

from app.app import app
from app.models.User import User
from tests.conftest import ORIGIN, register, ws_ready

HEADER = re.compile(r'^app;dur=\d+(\.\d+)?, db;dur=\d+(\.\d+)?;desc="(\d+) quer(y|ies)"$')


def query_count(response) -> int:
    match = HEADER.match(response.headers["server-timing"])
    assert match, response.headers["server-timing"]
    return int(match.group(3))


@pytest.fixture
def routes():
    async def queries(n: int):
        for _ in range(n):
            await User.all().count()
        return {"ok": True}

    async def interleaved(n: int, pause: float):
        for _ in range(n):
            await User.all().count()
            await anyio.sleep(pause)
        return {"ok": True}

    before = len(app.router.routes)
    app.add_api_route("/test-queries", queries, methods=["GET"])
    app.add_api_route("/test-interleaved", interleaved, methods=["GET"])
    yield
    del app.router.routes[before:]


def test_header_on_normal_and_error_responses(client):
    register(client)
    assert query_count(client.get("/auth/me")) > 0
    assert query_count(client.get("/servers/999999")) >= 0

    anonymous = client.get("/auth/me", cookies={"access_token": ""})
    assert anonymous.status_code == 401
    assert query_count(anonymous) >= 0


def test_counts_exactly_the_queries_a_request_runs(client, routes):
    assert query_count(client.get("/test-queries", params={"n": 3})) == 3
    assert query_count(client.get("/test-queries", params={"n": 1})) == 1
    assert query_count(client.get("/test-queries", params={"n": 0})) == 0


def test_identical_requests_report_the_same_count(client):
    register(client)
    counts = {query_count(client.get("/auth/me")) for _ in range(3)}
    assert len(counts) == 1


def test_session_lookup_is_counted(client, routes):
    anonymous = query_count(client.get("/test-queries", params={"n": 0}))
    register(client)
    # The token row, then its prefetched user.
    assert query_count(client.get("/test-queries", params={"n": 0})) == anonymous + 2


def test_concurrent_requests_do_not_share_a_counter(client, routes):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            results = {}

            async def fetch(name, n):
                results[name] = await http.get("/test-interleaved", params={"n": n, "pause": 0.01})

            async with anyio.create_task_group() as tg:
                tg.start_soon(fetch, "two", 2)
                tg.start_soon(fetch, "seven", 7)
            return results

    results = client.portal.call(run)
    assert query_count(results["two"]) == 2
    assert query_count(results["seven"]) == 7


def test_slow_request_logs_one_warning(client, routes, caplog):
    with caplog.at_level(logging.WARNING, logger="app.db_timing"):
        client.get("/test-queries", params={"n": 15})
        assert not caplog.records
        client.get("/test-queries", params={"n": 16})

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "GET /test-queries" in message
    assert "16 queries" in message


def test_cors_preflight_and_websocket_still_work(client):
    register(client)
    preflight = client.options(
        "/auth/me",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200

    with client.websocket_connect("/ws", headers={"Origin": ORIGIN}) as ws:
        ws_ready(ws)
        ws.send_json({"type": "ping", "t": 1})
        assert ws.receive_json() == {"type": "pong", "t": 1}
