import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from tortoise.functions import Count

from ..models.Message import Message
from ..models.Server import Server
from ..models.ServerRequest import ServerRequest
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..platform import require_platform_admin
from ..services import error_counter
from ..services.host_metrics import host_metrics
from ..services.voice_stats import voice_snapshot

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])

logger = logging.getLogger("app.admin")

TOP_SERVERS = 10
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


async def _voice() -> dict | None:
    stats = await voice_snapshot()
    return asdict(stats) if stats else None


async def _users(request: Request, now: datetime) -> dict:
    comms = request.app.state.comms.connection_manager
    online = comms.online_user_ids()
    return {
        "total": await User.all().count(),
        "new_7d": await User.filter(created_at__gte=now - timedelta(days=7)).count(),
        "active_now": sum(1 for user_id in online if comms.is_active(user_id)),
        "online": len(online),
        "dau": await User.filter(last_active_at__gte=now - timedelta(days=1)).count(),
        "wau": await User.filter(last_active_at__gte=now - timedelta(days=7)).count(),
    }


async def _servers(now: datetime) -> dict:
    # Channel messages only: DMs have no server and are never counted.
    busiest = await (
        Message.filter(created_at__gte=now - timedelta(days=1), server_id__isnull=False)
        .annotate(count=Count("id"))
        .group_by("server_id")
        .order_by("-count")
        .limit(TOP_SERVERS)
        .values("server_id", "count")
    )
    ids = [row["server_id"] for row in busiest]
    names = dict(await Server.filter(id__in=ids).values_list("id", "name"))
    members = {
        row["server_id"]: row["count"]
        for row in await UserToServer.filter(server_id__in=ids)
        .annotate(count=Count("id"))
        .group_by("server_id")
        .values("server_id", "count")
    }
    return {
        "total": await Server.all().count(),
        "pending_requests": await ServerRequest.filter(status="pending").count(),
        "top": [
            {
                "id": row["server_id"],
                "name": names.get(row["server_id"], ""),
                "members": members.get(row["server_id"], 0),
                "messages_24h": row["count"],
            }
            for row in busiest
        ],
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
    return {
        "generated_at": now.isoformat(),
        "load": await _section("load", _load),
        "voice": await _section("voice", _voice),
        "users": await _section("users", lambda: _users(request, now)),
        "servers": await _section("servers", lambda: _servers(now)),
        "errors": await _section("errors", _errors),
    }
