import base64
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..utils import require_membership

logger = logging.getLogger("app.routers.channels")

router = APIRouter(prefix="/channels", tags=["channels"])

# Bounds on how many messages a single request may return.
DELTA_SYNC_LIMIT = 500
MAX_DELTA_SYNC_LIMIT = 1000
HISTORY_PAGE_SIZE = 50
MAX_HISTORY_PAGE_SIZE = 100


def _encode_cursor(created_at: datetime, message_id: int) -> str:
    """Opaque cursor pointing just before a message, keyed by (created_at, id)."""
    raw = f"{created_at.isoformat()}|{message_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso, message_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(iso), int(message_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _serialize(message: dict) -> dict:
    return {
        "id": message["uuid"],
        "content": message["content"],
        "user_id": message["author_id"],
        "channel_id": message["channel_id"],
        "server_id": message["server_id"],
        "timestamp": message["timestamp"],
    }


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_settings: dict
    type: str

    @classmethod
    def from_channel(cls, channel: Channel):
        return cls(
            id=channel.id,
            name=channel.name,
            channel_settings=channel.channel_settings,
            type=channel.type,
        )



@router.get("/messages")
async def get_messages(
    last_updated: datetime,
    limit: int = DELTA_SYNC_LIMIT,
    current_user: User = Depends(get_current_user),
):
    """Fetch messages from servers the user belongs to, updated after last_updated.

    Used for reconnect catch-up. The limit is capped so a long absence can't
    pull down unbounded history in one request; older gaps are backfilled
    through the per-channel history endpoint instead.
    """
    limit = max(1, min(limit, MAX_DELTA_SYNC_LIMIT))
    user_servers = await UserToServer.filter(user=current_user).values_list(
        "server_id",
        flat=True,
    )

    messages = await Message.filter(
        server_id__in=user_servers,
        created_at__gt=last_updated,
    ).order_by("created_at").limit(limit).values(
        "uuid",
        "content",
        "author_id",
        "channel_id",
        "server_id",
        "timestamp",
    )

    return [_serialize(message) for message in messages]


@router.get("/{channel_id}/messages")
async def get_channel_messages(
    channel_id: int,
    before: Optional[str] = None,
    limit: int = HISTORY_PAGE_SIZE,
    current_user: User = Depends(get_current_user),
):
    """Newest-first page of a channel's history.

    Pass the previous page's next_cursor as before to walk further back.
    """
    limit = max(1, min(limit, MAX_HISTORY_PAGE_SIZE))

    channel = await Channel.get_or_none(id=channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    server = await Server.get_or_none(id=channel.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_membership(current_user, server)

    query = Message.filter(channel_id=channel_id)
    if before:
        cursor_created_at, cursor_id = _decode_cursor(before)
        query = query.filter(
            Q(created_at__lt=cursor_created_at)
            | Q(created_at=cursor_created_at, id__lt=cursor_id)
        )

    # Fetch one extra row to tell whether an older page exists.
    rows = await query.order_by("-created_at", "-id").limit(limit + 1).values(
        "id",
        "uuid",
        "content",
        "author_id",
        "channel_id",
        "server_id",
        "timestamp",
        "created_at",
    )

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])

    return {
        "messages": [_serialize(row) for row in rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/{server_id}", response_model=List[ChannelResponse])
async def get_channels(
    server_id: int,
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_membership(current_user, server)

    channels = await Channel.filter(server_id=server.id).all()
    return [ChannelResponse.from_channel(channel) for channel in channels]


@router.post("/{server_id}/create", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    server_id: int,
    channel_name: str,
    channel_type: str = "text",
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_membership(current_user, server)

    channel = await Channel.create(
        name=channel_name,
        channel_settings={},
        type=channel_type,
        server=server,
    )

    # Add the channel to the channel_order in server settings
    server_settings = server.server_settings or {}
    channel_order = server_settings.get("channel_order", [])
    channel_order.append(channel.id)
    server_settings["channel_order"] = channel_order
    server.server_settings = server_settings
    await server.save()

    return ChannelResponse.from_channel(channel)
