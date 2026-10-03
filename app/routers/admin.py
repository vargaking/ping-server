import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from tortoise.functions import Count

from ..models.Channel import Channel
from ..models.Message import Message
from ..models.Server import Server
from ..models.ServerRequest import ServerRequest
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..platform import require_platform_admin
from ..services import error_counter, recent_errors
from ..services.host_metrics import host_metrics
from ..services.stats_history import build_history, sampling_enabled
from ..services.voice_stats import voice_snapshot

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])

logger = logging.getLogger("app.admin")

DEFAULT_UPLINK_MBPS = 1000


def uplink_mbps() -> float:
    try:
        return float(os.getenv("UPLINK_MBPS", DEFAULT_UPLINK_MBPS))
    except ValueError:
        return DEFAULT_UPLINK_MBPS


async def _section(name: str, build: Callable[[], Awaitable[Any]]) -> Any:
    """Run one stats section; a failing source leaves that section null."""
    try:
        return await build()
    except Exception:
        logger.warning("Admin stats section %s failed", name, exc_info=True)
        return None


async def _load() -> dict:
    return {"uplink_mbps": uplink_mbps(), **asdict(host_metrics.sample())}


async def _users(request: Request, now: datetime) -> dict:
    comms = request.app.state.comms.connection_manager
    online, active = comms.online_and_active_counts()
    return {
        "total": await User.all().count(),
        "new_7d": await User.filter(created_at__gte=now - timedelta(days=7)).count(),
        "active_now": active,
        "online": online,
        "dau": await User.filter(last_active_at__gte=now - timedelta(days=1)).count(),
        "wau": await User.filter(last_active_at__gte=now - timedelta(days=7)).count(),
    }


def _counts(rows) -> dict[int, int]:
    return {row["server_id"]: row["count"] for row in rows}


async def _message_counts(**filters) -> dict[int, int]:
    # Channel messages only: DMs have no server and are never counted.
    return _counts(
        await Message.filter(server_id__isnull=False, **filters)
        .annotate(count=Count("id"))
        .group_by("server_id")
        .values("server_id", "count")
    )


async def _voice_counts(presence) -> dict[int, int]:
    if presence is None:
        return {}
    occupied = presence.occupancy()
    servers = dict(await Channel.filter(id__in=list(occupied)).values_list("id", "server_id"))
    counts: dict[int, int] = {}
    for channel_id, people in occupied.items():
        if (server_id := servers.get(channel_id)) is not None:
            counts[server_id] = counts.get(server_id, 0) + people
    return counts


async def _servers(request: Request, now: datetime) -> dict:
    servers = await Server.all().values("id", "name", "created_at")
    members = _counts(
        await UserToServer.annotate(count=Count("id"))
        .group_by("server_id")
        .values("server_id", "count")
    )
    messages_24h = await _message_counts(created_at__gte=now - timedelta(days=1))
    messages_total = await _message_counts()
    in_voice = await _voice_counts(getattr(request.app.state, "voice_presence", None))
    rows = [
        {
            "id": server["id"],
            "name": server["name"],
            "members": members.get(server["id"], 0),
            "messages_24h": messages_24h.get(server["id"], 0),
            "messages_total": messages_total.get(server["id"], 0),
            "in_voice": in_voice.get(server["id"], 0),
            "created_at": server["created_at"].isoformat(),
        }
        for server in servers
    ]
    rows.sort(key=lambda row: (-row["messages_24h"], row["name"].lower()))
    return {
        "total": len(rows),
        "pending_requests": await ServerRequest.filter(status="pending").count(),
        "list": rows,
    }


async def _errors() -> dict:
    return {
        "client_24h": error_counter.client_errors.count(),
        "server_5xx_24h": error_counter.server_5xx.count(),
        "since": error_counter.started_at.isoformat(),
    }


@router.get("/stats")
async def get_stats(request: Request) -> dict:
    """Aggregate numbers only: no usernames, message content or DM data."""
    now = datetime.now(timezone.utc)
    voice = await _section("voice", voice_snapshot)
    return {
        "generated_at": now.isoformat(),
        "load": await _section("load", _load),
        "voice": asdict(voice.stats) if voice and voice.status == "ok" and voice.stats else None,
        "voice_status": voice.status if voice else "unreachable",
        "users": await _section("users", lambda: _users(request, now)),
        "servers": await _section("servers", lambda: _servers(request, now)),
        "errors": await _section("errors", _errors),
    }


@router.get("/stats/history")
async def get_stats_history(range: Literal["1h", "24h", "7d", "30d"] = "1h") -> dict:
    """Bucketed averages and maxima per metric, aggregates only."""
    history = await build_history(range, datetime.now(timezone.utc))
    return {**history, "uplink_mbps": uplink_mbps(), "sampling": sampling_enabled()}


@router.get("/errors")
async def get_errors(source: Literal["client", "server"] | None = None) -> dict:
    entries = []
    if source in (None, "client"):
        entries += recent_errors.client.entries()
    if source in (None, "server"):
        entries += recent_errors.server.entries()
    return {
        "since": error_counter.started_at.isoformat(),
        "limit": recent_errors.MAX_ENTRIES,
        "groups": recent_errors.grouped(entries),
    }
