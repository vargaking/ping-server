"""GET /admin/stats: platform-admin dashboard numbers, aggregates only."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.Conversation import Conversation
from app.models.Message import Message
from app.models.ServerRequest import ServerRequest
from app.models.User import User
from app.routers import admin
from app.services import activity
from app.services.error_counter import ErrorCounter
from app.services.host_metrics import HostMetrics, HostStats
from app.services.voice_stats import VoiceStats
from tests.conftest import create_channel, create_server, register, ws_ready
from tests.test_invite_preview import invite_and_join  # noqa: F401
from tests.test_server_requests import make_admin

URL = "/admin/stats"
HOST = HostStats(
    cpu_percent=7.5, ram_used_bytes=2_000, ram_total_bytes=8_000,
    disk_free_bytes=50_000, disk_total_bytes=100_000, net_out_mbps=None, net_in_mbps=None)


@pytest.fixture(autouse=True)
def stub_voice(monkeypatch):
    async def none():
        return None
    monkeypatch.setattr(admin, "voice_snapshot", none)


def run(client, func, *args):
    return client.portal.call(func, *args)


def stats(client) -> dict:
    res = client.get(URL)
    assert res.status_code == 200, res.text
    return res.json()


def add_message(client, *, author_id, age=timedelta(0), **where):
    async def create():
        message = await Message.create(
            uuid=uuid.uuid4(), content="secret words", author_id=author_id,
            timestamp=datetime.now(timezone.utc), **where)
        await Message.filter(id=message.id).update(created_at=datetime.now(timezone.utc) - age)
    run(client, create)


def test_requires_platform_admin(client, new_client):
    assert client.get(URL).status_code == 401
    register(client, "plain-stats")
    assert client.get(URL).status_code == 403
    admin_client = new_client()
    make_admin(admin_client, "stats-admin")
    assert admin_client.get(URL).status_code == 200


def test_user_counts(client, new_client):
    make_admin(client, "count-admin")
    old = register(new_client(), "old-user")
    daily = register(new_client(), "daily-user")
    weekly = register(new_client(), "weekly-user")
    now = datetime.now(timezone.utc)

    async def seed():
        await User.filter(id=old["id"]).update(created_at=now - timedelta(days=30))
        await User.filter(id=daily["id"]).update(last_active_at=now - timedelta(hours=2))
        await User.filter(id=weekly["id"]).update(last_active_at=now - timedelta(days=3))
    run(client, seed)

    users = stats(client)["users"]
    assert users["total"] == 4
    assert users["new_7d"] == 3
    assert users["dau"] == 1
    assert users["wau"] == 2
    assert users["online"] == 0 and users["active_now"] == 0


def test_server_totals_ranking_and_dm_exclusion(client, new_client):
    owner = make_admin(client, "rank-admin")
    quiet = create_server(client, "Quiet")
    busy = create_server(client, "Busy")
    quiet_channel = create_channel(client, quiet["id"])
    busy_channel = create_channel(client, busy["id"])
    friend_client = new_client()
    friend = register(friend_client, "rank-friend")
    invite_and_join(client, friend_client, busy["id"])

    for _ in range(3):
        add_message(client, author_id=owner["id"], server_id=busy["id"], channel_id=busy_channel["id"])
    add_message(client, author_id=owner["id"], server_id=quiet["id"], channel_id=quiet_channel["id"])
    add_message(client, author_id=owner["id"], server_id=busy["id"], channel_id=busy_channel["id"],
                age=timedelta(days=2))

    async def add_dm():
        pair = Conversation(user_a_id=min(owner["id"], friend["id"]), user_b_id=max(owner["id"], friend["id"]))
        await pair.save()
        return pair.id
    conversation_id = run(client, add_dm)
    add_message(client, author_id=owner["id"], conversation_id=conversation_id)

    async def add_pending():
        await ServerRequest.create(
            user_id=friend["id"], name="Wanted", description="please", expected_size="lt10")
    run(client, add_pending)

    servers = stats(client)["servers"]
    assert servers["total"] == 2
    assert servers["pending_requests"] == 1
    assert servers["top"] == [
        {"id": busy["id"], "name": "Busy", "members": 2, "messages_24h": 3},
        {"id": quiet["id"], "name": "Quiet", "members": 1, "messages_24h": 1},
    ]


def test_touch_last_active_writes_once_an_hour(client, monkeypatch):
    me = register(client, "touch-user")
    now = [1000.0]
    monkeypatch.setattr(activity, "clock", lambda: now[0])

    async def last_active():
        return (await User.get(id=me["id"])).last_active_at

    async def reset():
        await User.filter(id=me["id"]).update(last_active_at=None)

    run(client, activity.touch_last_active, me["id"])
    first = run(client, last_active)
    assert first is not None

    run(client, reset)
    now[0] += 3599
    run(client, activity.touch_last_active, me["id"])
    assert run(client, last_active) is None

    now[0] += 2
    run(client, activity.touch_last_active, me["id"])
    assert run(client, last_active) is not None


def test_websocket_connect_marks_user_active(client):
    me = register(client, "ws-active")
    with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as ws:
        ws_ready(ws)

    async def last_active():
        return (await User.get(id=me["id"])).last_active_at
    assert run(client, last_active) is not None


def test_error_counts(client, monkeypatch):
    make_admin(client, "errors-admin")
    before = stats(client)["errors"]

    client.post("/api/client-errors", json={"kind": "error", "message": "boom"})

    async def boom():
        raise RuntimeError("kaboom")
    from app.app import app
    app.add_api_route("/test-stats-boom", boom, methods=["GET"])
    try:
        assert client.get("/test-stats-boom").status_code == 500
    finally:
        app.router.routes.pop()

    after = stats(client)["errors"]
    assert after["client_24h"] == before["client_24h"] + 1
    assert after["server_5xx_24h"] == before["server_5xx_24h"] + 1
    assert after["since"]


def test_error_counter_drops_events_older_than_a_day():
    now = [0.0]
    counter = ErrorCounter()
    counter.clock = lambda: now[0]
    counter.record()
    now[0] = 23 * 3600
    counter.record()
    assert counter.count() == 2
    now[0] = 25 * 3600
    assert counter.count() == 1


def test_voice_section_is_passed_through_or_null(client, monkeypatch):
    make_admin(client, "voice-admin")
    assert stats(client)["voice"] is None

    async def snapshot():
        return VoiceStats(rooms=1, participants=2, screenshares=[{"width": 1920, "height": 1080}])
    monkeypatch.setattr(admin, "voice_snapshot", snapshot)
    assert stats(client)["voice"] == {
        "rooms": 1, "participants": 2, "screenshares": [{"width": 1920, "height": 1080}]}


def test_load_section_uses_host_sample_and_uplink(client, monkeypatch):
    make_admin(client, "load-admin")
    monkeypatch.setenv("UPLINK_MBPS", "250")
    monkeypatch.setattr(admin.host_metrics, "sample", lambda: HOST)
    load = stats(client)["load"]
    assert load["uplink_mbps"] == 250
    assert load["cpu_percent"] == 7.5
    assert load["net_out_mbps"] is None and load["net_in_mbps"] is None
    assert load["disk_free_bytes"] == 50_000


def test_first_host_sample_has_no_network_rates():
    metrics = HostMetrics()
    first = metrics.sample()
    assert first.net_out_mbps is None and first.net_in_mbps is None
    second = metrics.sample()
    assert second.net_out_mbps is not None and second.net_in_mbps is not None
    assert 0 <= second.cpu_percent <= 100


def test_failing_section_is_null_and_the_rest_still_returns(client, monkeypatch):
    make_admin(client, "partial-admin")

    def broken():
        raise OSError("no disk")
    monkeypatch.setattr(admin.host_metrics, "sample", broken)
    body = stats(client)
    assert body["load"] is None
    assert body["users"]["total"] == 1


def test_response_has_no_content_or_user_details(client):
    owner = make_admin(client, "private-admin")
    server = create_server(client, "Private place")
    channel = create_channel(client, server["id"])
    add_message(client, author_id=owner["id"], server_id=server["id"], channel_id=channel["id"])

    body = json.dumps(stats(client))
    assert "secret words" not in body
    assert "private-admin" not in body
    for key in ("content", "username", "conversation", "author"):
        assert f'"{key}' not in body
