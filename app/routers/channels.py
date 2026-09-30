import base64
import logging
from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..permissions import (
    Permission,
    channel_from_path,
    check_permission,
    server_of_channel,
)
from ..services import read_state
from ..services.attachments import attachments_by_message
from ..services.voice_presence import close_voice_channels

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


def _serialize(message: dict, attachments: list[dict]) -> dict:
    return {
        "id": message["uuid"],
        "content": message["content"],
        "user_id": message["author_id"],
        "channel_id": message["channel_id"],
        "server_id": message["server_id"],
        "timestamp": message["timestamp"],
        "edited_at": message["edited_at"],
        "attachments": attachments,
    }


async def _serialize_all(rows: list[dict]) -> list[dict]:
    by_message = await attachments_by_message([row["id"] for row in rows])
    return [_serialize(row, by_message.get(row["id"], [])) for row in rows]


CHANNEL_NAME_MAX = 100
CHANNEL_TOPIC_MAX = 1024


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Channel name can't be empty")
    if len(value) > CHANNEL_NAME_MAX:
        raise ValueError(f"Channel name must be at most {CHANNEL_NAME_MAX} characters")
    return value


def _clean_topic(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if len(value) > CHANNEL_TOPIC_MAX:
        raise ValueError(f"Topic must be at most {CHANNEL_TOPIC_MAX} characters")
    # An empty topic clears it rather than storing "".
    return value or None


class ChannelCreate(BaseModel):
    name: str
    type: Literal["text", "voice"] = "text"
    topic: Optional[str] = None

    _name = field_validator("name")(_clean_name)
    _topic = field_validator("topic")(_clean_topic)


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    topic: Optional[str] = None
    # Accepted only so a client echoing the channel back gets a clear error
    # instead of a silent ignore: a channel's type is fixed at creation.
    type: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_name(value)

    _topic = field_validator("topic")(_clean_topic)


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_settings: dict
    type: str
    topic: Optional[str] = None
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
            topic=channel.topic,
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
        "id",
        "uuid",
        "content",
        "author_id",
        "channel_id",
        "server_id",
        "timestamp",
        "edited_at",
    )

    return await _serialize_all(messages)


@router.get("/{channel_id}/messages")
async def get_channel_messages(
    channel_id: int,
    before: Optional[str] = None,
    limit: int = HISTORY_PAGE_SIZE,
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, server_of_channel)),
):
    """Newest-first page of a channel's history.

    Pass the previous page's next_cursor as before to walk further back.
    """
    limit = max(1, min(limit, MAX_HISTORY_PAGE_SIZE))

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
        "messages": await _serialize_all(rows),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.put("/{channel_id}/read")
async def mark_channel_read(
    channel_id: int,
    body: ReadMarkerUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, server_of_channel)),
):
    """Advance the caller's read marker for this channel to *message_id*.

    The marker only ever moves forward: an older message_id than what is
    already stored is a no-op, and the response reflects the (unchanged)
    newer marker.
    """
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
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL)),
):
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


async def _broadcast(request: Request, server_id: int, frame: dict, exclude_user_id: int) -> None:
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(server_id, frame, exclude_user_id=exclude_user_id)


@router.post("/{server_id}/create", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    server_id: int,
    body: ChannelCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS)),
):
    channel = await Channel.create(
        name=body.name,
        channel_settings={},
        type=body.type,
        topic=body.topic,
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
    await _broadcast(request, server_id, {
        "type": "channel_created",
        "server_id": server_id,
        "channel": channel_response.model_dump(mode="json"),
    }, exclude_user_id=current_user.id)

    return channel_response


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    body: ChannelUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    channel: Channel = Depends(channel_from_path),
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
):
    """Rename a channel or change its topic. The type is fixed."""

    changes = body.model_dump(exclude_unset=True)
    if "type" in changes and changes.pop("type") != channel.type:
        raise HTTPException(status_code=400, detail="A channel's type can't be changed")
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Channel name can't be empty")

    if changes:
        await channel.update_from_dict(changes)
        await channel.save(update_fields=list(changes))

    channel_response = ChannelResponse.from_channel(channel)
    if changes:
        # Read-state fields are per user, so they are left out of the frame;
        # clients keep their own values and patch the rest.
        await _broadcast(request, server.id, {
            "type": "channel_updated",
            "server_id": server.id,
            "channel": channel_response.model_dump(
                mode="json", exclude={"last_read_message_id", "last_message_id"}),
        }, exclude_user_id=current_user.id)

    return channel_response


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    channel: Channel = Depends(channel_from_path),
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
):
    """Delete a channel and all of its messages."""

    was_voice = channel.type == "voice"
    async with in_transaction():
        # Messages cascade at the DB level too, but deleting them explicitly
        # keeps this independent of how the FK was migrated.
        await Message.filter(channel_id=channel.id).delete()
        await channel.delete()

        server = await Server.get(id=server.id)
        server_settings = server.server_settings or {}
        order = server_settings.get("channel_order")
        if order and channel_id in order:
            server_settings["channel_order"] = [cid for cid in order if cid != channel_id]
            server.server_settings = server_settings
            await server.save(update_fields=["server_settings"])

    await _broadcast(request, server.id, {
        "type": "channel_deleted",
        "server_id": server.id,
        "channel_id": channel_id,
    }, exclude_user_id=current_user.id)

    if was_voice:
        await close_voice_channels(
            getattr(request.app.state, "voice_presence", None), [channel_id])
