"""Stats sampler, retention and GET /admin/stats/history."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.models.StatSample import StatSample
from app.routers import admin
from app.services import stats_history
from app.services.host_metrics import HostStats
from app.services.stats_history import METRICS, StatsSampler, floor_to
from app.services.voice_stats import VoiceReading, VoiceStats
from tests.conftest import register
from tests.test_server_requests import make_admin

URL = "/admin/stats/history"
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def host_stats(**overrides) -> HostStats:
    values = dict(
        cpu_percent=10.0, ram_used_bytes=2_000, ram_total_bytes=8_000,
        disk_free_bytes=1, disk_total_bytes=2, net_out_mbps=5.0, net_in_mbps=2.0)
    return HostStats(**{**values, **overrides})


class FakeHost:
    def __init__(self, stats: HostStats) -> None:
        self.stats = stats

    def sample(self) -> HostStats:
        return self.stats


class FakeConnections:
    def online_and_active_counts(self):
        return 3, 2


def make_sampler(host=None) -> StatsSampler:
    comms = SimpleNamespace(connection_manager=FakeConnections())
    return StatsSampler(comms, host or FakeHost(host_stats()))


@pytest.fixture(autouse=True)
def voice(monkeypatch):
    reading = VoiceReading("ok", VoiceStats(rooms=1, participants=4, screenshares=[{}, {}]))

    async def snapshot():
        return reading
    monkeypatch.setattr(stats_history, "voice_snapshot", snapshot)
    monkeypatch.setattr(admin, "voice_snapshot", snapshot)


def run(client, func, *args):
    return client.portal.call(func, *args)


def rows(client, **where) -> list[StatSample]:
    async def load():
        return await StatSample.filter(**where).order_by("bucket", "metric")
    return run(client, load)


def seed(client, metric, bucket, avg, peak=None, resolution="minute"):
    async def create():
        await StatSample.create(
            metric=metric, resolution=resolution, bucket=bucket, avg=avg,
            max=avg if peak is None else peak)
    run(client, create)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def test_sample_once_writes_one_row_per_metric_and_is_idempotent(client):
    sampler = make_sampler()
    now = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)
    run(client, sampler.sample_once, now)
    run(client, sampler.sample_once, now + timedelta(seconds=5))

    saved = rows(client)
    assert sorted(row.metric for row in saved) == sorted(METRICS)
    by_metric = {row.metric: row for row in saved}
    assert by_metric["ram_percent"].avg == 25.0
    assert by_metric["voice_participants"].avg == 4
    assert by_metric["screenshares"].avg == 2
    assert by_metric["online_users"].avg == 3 and by_metric["active_users"].avg == 2
    assert all(row.avg == row.max and row.resolution == "minute" for row in saved)
    assert {row.bucket for row in saved} == {datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)}


def test_unavailable_sources_are_skipped(client, monkeypatch):
    sampler = make_sampler(FakeHost(host_stats(net_out_mbps=None, net_in_mbps=None)))
    monkeypatch.setattr(stats_history, "voice_snapshot", _voice_down)
    run(client, sampler.sample_once, now_utc())
    assert {row.metric for row in rows(client)} == {
        "cpu_percent", "ram_percent", "online_users", "active_users"}


async def _voice_down():
    return VoiceReading("unreachable")


def test_voice_raising_does_not_stop_other_metrics(client, monkeypatch):
    async def broken():
        raise RuntimeError("boom")
    monkeypatch.setattr(stats_history, "voice_snapshot", broken)
    reading = run(client, make_sampler().collect)
    assert "voice_rooms" not in reading
    assert reading["cpu_percent"] == 10.0 and reading["online_users"] == 3


def test_rollup_aggregates_complete_hours_once(client):
    hour = floor_to(now_utc(), HOUR) - 3 * HOUR
    for minute, value in enumerate((2.0, 4.0, 9.0)):
        seed(client, "cpu_percent", hour + minute * MINUTE, value)
    for minute, value in enumerate((1.0, 3.0)):
        seed(client, "cpu_percent", hour + HOUR + minute * MINUTE, value)
    current = floor_to(now_utc(), HOUR)
    seed(client, "cpu_percent", current, 50.0)

    sampler = make_sampler()
    run(client, sampler.rollup, now_utc())
    run(client, sampler.rollup, now_utc())

    hours = rows(client, resolution="hour")
    assert [(row.bucket, row.avg, row.max) for row in hours] == [
        (hour, 5.0, 9.0), (hour + HOUR, 2.0, 3.0)]


def test_prune_removes_only_expired_rows(client):
    now = now_utc()
    seed(client, "cpu_percent", now - timedelta(days=8), 1.0)
    seed(client, "cpu_percent", now - timedelta(days=6), 2.0)
    seed(client, "cpu_percent", now - timedelta(days=91), 3.0, resolution="hour")
    seed(client, "cpu_percent", now - timedelta(days=60), 4.0, resolution="hour")

    run(client, make_sampler().prune, now)

    assert sorted(row.avg for row in rows(client)) == [2.0, 4.0]


def test_history_requires_platform_admin(client, new_client):
    assert client.get(URL).status_code == 401
    register(client, "history-plain")
    assert client.get(URL).status_code == 403


def test_invalid_range_is_rejected(client):
    make_admin(client, "history-range")
    assert client.get(URL, params={"range": "5y"}).status_code == 422


@pytest.mark.parametrize("range_name, step, count", [
    ("1h", 60, 61), ("24h", 300, 289), ("7d", 3600, 169), ("30d", 3600, 721),
])
def test_range_shapes(client, range_name, step, count):
    make_admin(client, "history-shape")
    body = client.get(URL, params={"range": range_name}).json()
    assert body["range"] == range_name
    assert body["bucket_seconds"] == step
    assert len(body["buckets"]) == count
    assert set(body["series"]) == set(METRICS) == set(body["peaks"])
    for series in body["series"].values():
        assert len(series["avg"]) == len(series["max"]) == count
        assert all(value is None for value in series["avg"] + series["max"])
    assert all(peak is None for peak in body["peaks"].values())
    assert body["uplink_mbps"] == 1000
    assert body["sampling"] is False


def test_sampling_flag_follows_env(client, monkeypatch):
    make_admin(client, "history-flag")
    monkeypatch.setenv("STATS_SAMPLING", "true")
    assert client.get(URL).json()["sampling"] is True


def test_minute_history_has_gaps_and_peak(client):
    make_admin(client, "history-minutes")
    base = floor_to(now_utc(), MINUTE)
    seed(client, "net_out_mbps", base - 5 * MINUTE, 10.0)
    seed(client, "net_out_mbps", base - 3 * MINUTE, 40.0)
    seed(client, "net_out_mbps", base - 2 * MINUTE, 20.0)

    body = client.get(URL, params={"range": "1h"}).json()

    out = body["series"]["net_out_mbps"]
    assert out["max"][-6] == 10.0 and out["max"][-4] == 40.0
    assert out["max"][-5] is None and out["avg"][-5] is None
    assert body["peaks"]["net_out_mbps"] == {
        "value": 40.0, "at": (base - 3 * MINUTE).isoformat().replace("+00:00", "Z")}
    assert body["peaks"]["cpu_percent"] is None


def test_day_history_rebuckets_avg_and_max(client):
    make_admin(client, "history-day")
    start = floor_to(now_utc(), timedelta(minutes=5)) - timedelta(minutes=30)
    seed(client, "cpu_percent", start, 10.0)
    seed(client, "cpu_percent", start + MINUTE, 30.0)
    seed(client, "cpu_percent", start + 4 * MINUTE, 20.0)

    body = client.get(URL, params={"range": "24h"}).json()

    index = body["buckets"].index(start.isoformat().replace("+00:00", "Z"))
    assert body["series"]["cpu_percent"]["avg"][index] == 20.0
    assert body["series"]["cpu_percent"]["max"][index] == 30.0


def test_week_history_mixes_hours_with_current_partial_hour(client):
    make_admin(client, "history-week")
    current = floor_to(now_utc(), HOUR)
    seed(client, "online_users", current - 2 * HOUR, 3.0, 7.0, resolution="hour")
    seed(client, "online_users", current, 4.0)

    body = client.get(URL, params={"range": "7d"}).json()

    users = body["series"]["online_users"]
    assert users["max"][-3] == 7.0 and users["avg"][-3] == 3.0
    assert users["max"][-2] is None
    assert users["max"][-1] == 4.0
    assert body["peaks"]["online_users"]["value"] == 7.0
    assert body["peaks"]["online_users"]["at"] == (current - 2 * HOUR).isoformat().replace("+00:00", "Z")


class SamplerRecorder:
    started = 0

    def __init__(self, comms):
        self.comms = comms

    async def run(self):
        type(self).started += 1
        await asyncio.sleep(3600)


def test_sampler_stays_off_unless_enabled(monkeypatch, app_lifespan):
    monkeypatch.delenv("STATS_SAMPLING", raising=False)
    monkeypatch.setattr("app.utils.StatsSampler", SamplerRecorder)
    SamplerRecorder.started = 0
    with TestClient(app) as started:
        assert started.app.state.stats_task is None
    assert SamplerRecorder.started == 0


def test_sampler_starts_when_enabled(monkeypatch, app_lifespan):
    monkeypatch.setenv("STATS_SAMPLING", "yes")
    monkeypatch.setattr("app.utils.StatsSampler", SamplerRecorder)
    SamplerRecorder.started = 0
    with TestClient(app) as started:
        assert started.app.state.stats_task is not None
    assert SamplerRecorder.started == 1
