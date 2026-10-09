import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..models.StatSample import StatSample
from .host_metrics import HostMetrics
from .voice_stats import voice_snapshot

logger = logging.getLogger("app.stats_history")

SAMPLE_SECONDS = 60
MINUTE_RETENTION = timedelta(days=7)
HOUR_RETENTION = timedelta(days=90)

SCOPE = "platform"
MINUTE = "minute"
HOUR = "hour"

METRICS = (
    "net_out_mbps",
    "net_in_mbps",
    "cpu_percent",
    "ram_percent",
    "online_users",
    "active_users",
    "voice_rooms",
    "voice_participants",
    "screenshares",
)


def sampling_enabled() -> bool:
    return os.getenv("STATS_SAMPLING", "").strip().lower() in ("true", "1", "yes")


def floor_to(moment: datetime, step: timedelta) -> datetime:
    seconds = int(step.total_seconds())
    return datetime.fromtimestamp(moment.timestamp() // seconds * seconds, timezone.utc)


class StatsSampler:
    """Writes one aggregate reading per minute, rolls minutes up to hours and
    prunes old rows. Several processes sampling the same minute is harmless:
    rows are unique per bucket and conflicts are ignored."""

    clock = staticmethod(lambda: datetime.now(timezone.utc))

    def __init__(self, comms, host: HostMetrics | None = None) -> None:
        self.comms = comms
        # Its own instance, so the page's polling does not shorten the window
        # the network rate is averaged over.
        self.host = host or HostMetrics()

    async def collect(self) -> dict[str, float]:
        reading: dict[str, float] = {}
        for name, read in (
            ("host", self._host_metrics),
            ("users", self._user_metrics),
            ("voice", self._voice_metrics),
        ):
            try:
                reading.update(await read())
            except Exception:
                logger.warning("Stats source %s failed", name, exc_info=True)
        return reading

    async def _host_metrics(self) -> dict[str, float]:
        stats = self.host.sample()
        values = {
            "net_out_mbps": stats.net_out_mbps,
            "net_in_mbps": stats.net_in_mbps,
            "cpu_percent": stats.cpu_percent,
            "ram_percent": stats.ram_used_bytes / stats.ram_total_bytes * 100
            if stats.ram_total_bytes
            else None,
        }
        return {name: value for name, value in values.items() if value is not None}

    async def _user_metrics(self) -> dict[str, float]:
        online, active = self.comms.connection_manager.online_and_active_counts()
        return {"online_users": online, "active_users": active}

    async def _voice_metrics(self) -> dict[str, float]:
        voice = await voice_snapshot()
        if voice.status != "ok" or voice.stats is None:
            return {}
        return {
            "voice_rooms": voice.stats.rooms,
            "voice_participants": voice.stats.participants,
            "screenshares": len(voice.stats.screenshares),
        }

    async def sample_once(self, now: datetime) -> None:
        bucket = floor_to(now, timedelta(seconds=SAMPLE_SECONDS))
        rows = [
            StatSample(scope=SCOPE, metric=metric, resolution=MINUTE, bucket=bucket,
                       avg=value, max=value)
            for metric, value in (await self.collect()).items()
        ]
        if rows:
            await StatSample.bulk_create(rows, ignore_conflicts=True)

    async def rollup(self, now: datetime) -> None:
        current_hour = floor_to(now, timedelta(hours=1))
        since = current_hour - MINUTE_RETENTION
        minutes = await StatSample.filter(
            scope=SCOPE, resolution=MINUTE, bucket__gte=since, bucket__lt=current_hour,
        ).values_list("metric", "bucket", "avg", "max")
        done = set(await StatSample.filter(
            scope=SCOPE, resolution=HOUR, bucket__gte=since, bucket__lt=current_hour,
        ).values_list("metric", "bucket"))

        grouped: dict[tuple[str, datetime], list[tuple[float, float]]] = defaultdict(list)
        for metric, bucket, avg, peak in minutes:
            key = (metric, floor_to(bucket, timedelta(hours=1)))
            if key not in done:
                grouped[key].append((avg, peak))

        rows = [
            StatSample(
                scope=SCOPE, metric=metric, resolution=HOUR, bucket=hour,
                avg=sum(avg for avg, _ in values) / len(values),
                max=max(peak for _, peak in values),
            )
            for (metric, hour), values in grouped.items()
        ]
        if rows:
            await StatSample.bulk_create(rows, ignore_conflicts=True)

    async def prune(self, now: datetime) -> None:
        await StatSample.filter(
            scope=SCOPE, resolution=MINUTE, bucket__lt=now - MINUTE_RETENTION).delete()
        await StatSample.filter(
            scope=SCOPE, resolution=HOUR, bucket__lt=now - HOUR_RETENTION).delete()

    async def housekeeping(self, now: datetime) -> None:
        await self.rollup(now)
        await self.prune(now)

    async def run(self) -> None:
        last_hour: datetime | None = None
        while True:
            try:
                now = self.clock()
                if last_hour is None or floor_to(now, timedelta(hours=1)) != last_hour:
                    await self.housekeeping(now)
                    last_hour = floor_to(now, timedelta(hours=1))
                await self.sample_once(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Stats sampling failed", exc_info=True)
            now = self.clock()
            next_minute = floor_to(now, timedelta(seconds=SAMPLE_SECONDS)) + timedelta(
                seconds=SAMPLE_SECONDS + 1)
            await asyncio.sleep((next_minute - now).total_seconds())


# range -> (window, bucket size, row resolution)
RANGES: dict[str, tuple[timedelta, timedelta, str]] = {
    "1h": (timedelta(hours=1), timedelta(seconds=60), MINUTE),
    "24h": (timedelta(hours=24), timedelta(seconds=300), MINUTE),
    "7d": (timedelta(days=7), timedelta(hours=1), HOUR),
    "30d": (timedelta(days=30), timedelta(hours=1), HOUR),
}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


async def _history_rows(resolution: str, start: datetime, now: datetime):
    rows = await StatSample.filter(
        scope=SCOPE, resolution=resolution, bucket__gte=start, bucket__lte=now,
    ).values_list("metric", "bucket", "avg", "max")
    if resolution == MINUTE:
        return rows
    # Hours that are not complete have no hour row yet, only minute rows.
    current_hour = floor_to(now, timedelta(hours=1))
    partial = await StatSample.filter(
        scope=SCOPE, resolution=MINUTE, bucket__gte=current_hour, bucket__lte=now,
    ).values_list("metric", "bucket", "avg", "max")
    return [*rows, *partial]


async def build_history(range_name: str, now: datetime) -> dict:
    window, step, resolution = RANGES[range_name]
    start = floor_to(now, step) - window
    count = int(window / step) + 1
    buckets = [start + step * i for i in range(count)]

    values: dict[str, list[list[tuple[float, float]]]] = {
        metric: [[] for _ in buckets] for metric in METRICS}
    peaks: dict[str, tuple[float, datetime] | None] = dict.fromkeys(METRICS)
    for metric, bucket, avg, peak in await _history_rows(resolution, start, now):
        if metric not in values:
            continue
        bucket = _as_utc(bucket)
        values[metric][int((bucket - start) / step)].append((avg, peak))
        best = peaks[metric]
        if best is None or peak > best[0] or (peak == best[0] and bucket < best[1]):
            peaks[metric] = (peak, bucket)

    series = {
        metric: {
            "avg": [sum(a for a, _ in cell) / len(cell) if cell else None for cell in cells],
            "max": [max(p for _, p in cell) if cell else None for cell in cells],
        }
        for metric, cells in values.items()
    }
    return {
        "range": range_name,
        "bucket_seconds": int(step.total_seconds()),
        "from": iso(start),
        "to": iso(now),
        "buckets": [iso(bucket) for bucket in buckets],
        "series": series,
        "peaks": {
            metric: {"value": best[0], "at": iso(best[1])} if best else None
            for metric, best in peaks.items()
        },
    }
