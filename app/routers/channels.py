import base64
import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..services import read_state
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
        "edited_at": message["edited_at"],
    }


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_settings: dict
    type: str
    last_read_message_id: Optional[str] = None
    last_message_id: Optional[str] = None

    @classmethod
    def from_channel(
        cls,
        channel: Channel,
        *,
        last_read_message_id: Optional[str] = None,
        last_message_id: Optional[str] = None,
    ):
        return cls(
            id=channel.id,
            name=channel.name,
            channel_settings=channel.channel_settings,
            type=channel.type,
            last_read_message_id=last_read_message_id,
            last_message_id=last_message_id,
        )


class ReadMarkerUpdate(BaseModel):
    message_id: str



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
        "edited_at",
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
        "edited_at",
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


@router.put("/{channel_id}/read")
async def mark_channel_read(
    channel_id: int,
    body: ReadMarkerUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Advance the caller's read marker for this channel to *message_id*.

    The marker only ever moves forward: an older message_id than what is
    already stored is a no-op, and the response reflects the (unchanged)
    newer marker.
    """
    channel = await Channel.get_or_none(id=channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    server = await Server.get_or_none(id=channel.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_membership(current_user, server)

    try:
        message_uuid = UUID(body.message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")

    message = await Message.get_or_none(uuid=message_uuid, channel_id=channel_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    moved = await read_state.advance(
        current_user.id, channel_id=channel_id, message_pk=message.id)

    marker = await read_state.get_marker(current_user.id, channel_id=channel_id)
    effective_uuid = await read_state.resolve_marker_uuid(marker, channel_id=channel_id)

    if moved:
        comms = getattr(request.app.state, "comms", None)
        if comms is not None:
            await comms.send_to_user(current_user.id, {
                "type": "read_state",
                "channel_id": channel_id,
                "server_id": server.id,
                "conversation_id": None,
                "last_read_message_id": effective_uuid,
            })

    return {
        "channel_id": channel_id,
        "server_id": server.id,
        "last_read_message_id": effective_uuid,
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
    state = await read_state.batch_channel_state(
        current_user.id, [c.id for c in channels])
    return [
        ChannelResponse.from_channel(
            channel,
            last_read_message_id=state.get(channel.id, {}).get("last_read_message_id"),
            last_message_id=state.get(channel.id, {}).get("last_message_id"),
        )
        for channel in channels
    ]


@router.post("/{server_id}/create", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    server_id: int,
    request: Request,
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

    channel_response = ChannelResponse.from_channel(channel)

    # Tell everyone else in the server so their channel list patches in place.
    # The creator already has it from this response, so skip them.
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(
            server_id,
            {
                "type": "channel_created",
                "server_id": server_id,
                "channel": channel_response.model_dump(mode="json"),
            },
            exclude_user_id=current_user.id,
        )

    return channel_response
