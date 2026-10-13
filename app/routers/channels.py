import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, StrictInt, field_validator
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.ForumPost import ForumPost
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..permissions import (
    Permission,
    channel_from_path,
    check_channel,
    check_permission,
)
from ..services import channel_layout, read_state
from ..services.channel_visibility import private_flags, visibility_change
from ..services.permissions import permissions
from ..services.server_queue import server_queue
from ..services.attachments import attachments_by_message
from ..services.reactions import reactions_by_message
from ..services.replies import reply_json, reply_refs
from ..services.voice_presence import close_voice_channels

logger = logging.getLogger("app.routers.channels")

router = APIRouter(prefix="/channels", tags=["channels"])

# Bounds on how many messages a single request may return.
HISTORY_PAGE_SIZE = 50
MAX_HISTORY_PAGE_SIZE = 100

CHANNEL_FIELDS = (
    "id",
    "uuid",
    "content",
    "author_id",
    "channel_id",
    "server_id",
    "post_id",
    "timestamp",
    "created_at",
    "edited_at",
    "reply_to_uuid",
    "metadata",
)


def _encode_cursor(at: datetime, row_id: int) -> str:
    """Opaque cursor pointing just before a row, keyed by (time, id)."""
    raw = f"{at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso, message_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(iso), int(message_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _older_than(order_field: str, at: datetime, row_id: int) -> Q:
    # The plain range on order_field is what lets the database seek into an
    # index; the NOT trims the ties at `at` down to ids below row_id.
    return Q(**{f"{order_field}__lte": at}) \
        & ~(Q(**{order_field: at}) & Q(id__gte=row_id))


def _newer_than(order_field: str, at: datetime, row_id: int) -> Q:
    return Q(**{f"{order_field}__gte": at}) \
        & ~(Q(**{order_field: at}) & Q(id__lte=row_id))


def _not_older_than(order_field: str, at: datetime, row_id: int) -> Q:
    return Q(**{f"{order_field}__gte": at}) \
        & ~(Q(**{order_field: at}) & Q(id__lt=row_id))


@dataclass
class HistoryPage:
    rows: list[dict]  # newest first
    next_cursor: Optional[str]
    has_more: bool
    has_newer: bool


def _message_id(value: Optional[str]) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Not a message id")


async def history_page(
    query,
    order_field: str,
    fields: tuple[str, ...],
    *,
    before: Optional[str],
    around: Optional[str],
    after: Optional[str],
    limit: int,
) -> HistoryPage:
    """One page of a thread's history; query is already scoped to the thread.

    before and after walk away from a position, around centres a window on
    one message. Every mode returns its rows newest first. around and after
    are parsed here rather than by FastAPI so that access is checked first.
    """
    around, after = _message_id(around), _message_id(after)
    if sum((bool(before), around is not None, after is not None)) > 1:
        raise HTTPException(
            status_code=422, detail="Use only one of before, around and after")

    anchor_uuid = around or after
    anchor = None
    if anchor_uuid is not None:
        anchor = await query.filter(uuid=anchor_uuid).first().values(*fields)
        if anchor is None:
            raise HTTPException(status_code=404, detail="Message not found")
        anchor_at, anchor_id = anchor[order_field], anchor["id"]

    newest_first = (f"-{order_field}", "-id")
    oldest_first = (order_field, "id")

    if around is not None:
        older_n = limit // 2
        newer_n = limit - 1 - older_n
        older = await query.filter(_older_than(order_field, anchor_at, anchor_id)) \
            .order_by(*newest_first).limit(older_n + 1).values(*fields)
        newer = await query.filter(_newer_than(order_field, anchor_at, anchor_id)) \
            .order_by(*oldest_first).limit(newer_n + 1).values(*fields)
        rows = newer[:newer_n][::-1] + [anchor] + older[:older_n]
        has_more = len(older) > older_n
        has_newer = len(newer) > newer_n
    elif after is not None:
        newer = await query.filter(_newer_than(order_field, anchor_at, anchor_id)) \
            .order_by(*oldest_first).limit(limit + 1).values(*fields)
        rows = newer[:limit][::-1]
        has_more = True
        has_newer = len(newer) > limit
        # Empty page: the cursor sits at the anchor, which the client holds.
        oldest = rows[-1] if rows else anchor
        return HistoryPage(
            rows, _encode_cursor(oldest[order_field], oldest["id"]), has_more, has_newer)
    else:
        has_newer = False
        page_query = query
        if before:
            cursor_at, cursor_id = _decode_cursor(before)
            older_than_cursor = _older_than(order_field, cursor_at, cursor_id)
            page_query = query.filter(older_than_cursor)
            has_newer = await query.filter(
                _not_older_than(order_field, cursor_at, cursor_id)).exists()
        # Fetch one extra row to tell whether an older page exists.
        rows = await page_query.order_by(*newest_first).limit(limit + 1).values(*fields)
        has_more = len(rows) > limit
        rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_cursor(rows[-1][order_field], rows[-1]["id"])
    return HistoryPage(rows, next_cursor, has_more, has_newer)


def _serialize(
    message: dict, attachments: list[dict], reactions: list[dict], refs: dict[UUID, dict]
) -> dict:
    return {
        "id": message["uuid"],
        "content": message["content"],
        "user_id": message["author_id"],
        "channel_id": message["channel_id"],
        "server_id": message["server_id"],
        "post_id": message["post_id"],
        "timestamp": message["timestamp"],
        "edited_at": message["edited_at"],
        "attachments": attachments,
        "reactions": reactions,
        "reply_to": reply_json(message["reply_to_uuid"], refs),
        "embeds": (message["metadata"] or {}).get("embeds", []),
        "imported_author": (message["metadata"] or {}).get("imported_author"),
    }


async def _serialize_all(rows: list[dict]) -> list[dict]:
    ids = [row["id"] for row in rows]
    attachments = await attachments_by_message(ids)
    reactions = await reactions_by_message(ids)
    refs = await reply_refs(row["reply_to_uuid"] for row in rows)
    return [
        _serialize(
            row, attachments.get(row["id"], []), reactions.get(row["id"], []), refs)
        for row in rows
    ]


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
    type: Literal["text", "voice", "forum"] = "text"
    topic: Optional[str] = None
    group_id: Optional[StrictInt] = None

    _name = field_validator("name")(_clean_name)
    _topic = field_validator("topic")(_clean_topic)


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    topic: Optional[str] = None
    # Accepted only so a client echoing the channel back gets a clear error
    # instead of a silent ignore: a channel's type is fixed at creation.
    type: Optional[str] = None
    group_id: Optional[StrictInt] = None

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
    group_id: Optional[int] = None
    position: int = 0
    last_read_message_id: Optional[str] = None
    last_message_id: Optional[str] = None
    mention_count: int = 0
    # @everyone can't view it.
    private: bool = False

    @classmethod
    def from_channel(
        cls,
        channel: Channel,
        *,
        last_read_message_id: Optional[str] = None,
        last_message_id: Optional[str] = None,
        mention_count: int = 0,
        private: bool = False,
    ):
        return cls(
            id=channel.id,
            name=channel.name,
            channel_settings=channel.channel_settings,
            type=channel.type,
            topic=channel.topic,
            group_id=channel.group_id,
            position=channel.position,
            last_read_message_id=last_read_message_id,
            last_message_id=last_message_id,
            mention_count=mention_count,
            private=private,
        )


PER_USER_FIELDS = {"last_read_message_id", "last_message_id", "mention_count"}


class ReadMarkerUpdate(BaseModel):
    message_id: str


@router.get("/{channel_id}/messages")
async def get_channel_messages(
    channel_id: int,
    before: Optional[str] = None,
    around: Optional[str] = None,
    after: Optional[str] = None,
    limit: int = HISTORY_PAGE_SIZE,
    post_id: Optional[int] = None,
    channel: Channel = Depends(channel_from_path),
    server: Server = Depends(check_channel()),
):
    """Newest-first page of a channel's history, or of one post's messages
    in a forum channel (which needs post_id; no other channel takes it).

    Pass the previous page's next_cursor as before to walk further back.
    around (a message id) returns a window centred on that message, after
    the messages newer than it. Use at most one of before, around and after.
    """
    limit = max(1, min(limit, MAX_HISTORY_PAGE_SIZE))

    in_forum = channel.type == "forum"
    if in_forum != (post_id is not None):
        raise HTTPException(
            status_code=422,
            detail="post_id is required in a forum channel and not allowed elsewhere")
    if in_forum and not await ForumPost.filter(id=post_id, channel_id=channel_id).exists():
        raise HTTPException(status_code=404, detail="Post not found")

    # A post is ordered by the message's own timestamp, which imported
    # history sets; everything else by arrival.
    page = await history_page(
        Message.filter(channel_id=channel_id, post_id=post_id),
        "timestamp" if in_forum else "created_at",
        CHANNEL_FIELDS,
        before=before, around=around, after=after, limit=limit,
    )

    return {
        "messages": await _serialize_all(page.rows),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "has_newer": page.has_newer,
    }


@router.put("/{channel_id}/read")
async def mark_channel_read(
    channel_id: int,
    body: ReadMarkerUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_channel()),
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
    view = await permissions.view(current_user.id, server)
    channels = [
        channel for channel in await Channel.filter(server_id=server.id).all()
        if view.can_view(channel.id)]
    state = await read_state.batch_channel_state(
        current_user.id, [c.id for c in channels])
    mentions = await read_state.mention_counts(current_user.id, [c.id for c in channels])
    private_channels, _ = await private_flags(server.id)
    return [
        ChannelResponse.from_channel(
            channel,
            last_read_message_id=state.get(channel.id, {}).get("last_read_message_id"),
            last_message_id=state.get(channel.id, {}).get("last_message_id"),
            mention_count=mentions.get(channel.id, 0),
            private=channel.id in private_channels,
        )
        for channel in channels
    ]


async def _check_group(server_id: int, group_id: Optional[int]) -> None:
    if group_id is not None and not await ChannelGroup.filter(
            id=group_id, server_id=server_id).exists():
        raise HTTPException(status_code=422, detail="Category not found in this server")


async def _broadcast(request: Request, server_id: int, frame: dict, exclude_user_id: int) -> None:
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(server_id, frame, exclude_user_id=exclude_user_id)


async def _broadcast_to_channel(
    request: Request, channel: Channel, frame: dict, exclude_user_id: int | None
) -> None:
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_channel(
            channel.server_id, channel.id, frame, exclude_user_id=exclude_user_id)


async def channel_response(channel: Channel) -> ChannelResponse:
    private_channels, _ = await private_flags(channel.server_id)
    return ChannelResponse.from_channel(channel, private=channel.id in private_channels)


@router.post("/{server_id}/create", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    server_id: int,
    body: ChannelCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS)),
):
    # Members who can view the new channel get channel_created from the
    # visibility change, so one created in a private category stays private.
    async with visibility_change(
            request.app.state, server, exclude_user_id=current_user.id):
        async with channel_layout.locked_server(server_id):
            if await Channel.filter(server_id=server_id).count() >= channel_layout.MAX_CHANNELS:
                raise HTTPException(
                    status_code=422,
                    detail=f"A server can have at most {channel_layout.MAX_CHANNELS} channels")
            await _check_group(server_id, body.group_id)
            channel = await Channel.create(
                name=body.name,
                channel_settings={},
                type=body.type,
                topic=body.topic,
                server=server,
                group_id=body.group_id,
                position=await channel_layout.next_channel_position(server_id, body.group_id),
            )

    return await channel_response(channel)


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    body: ChannelUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    channel: Channel = Depends(channel_from_path),
    server: Server = Depends(check_channel(Permission.MANAGE_CHANNELS)),
):
    """Rename a channel, change its topic or move it to another category.
    The type is fixed."""

    changes = body.model_dump(exclude_unset=True)
    if "type" in changes and changes.pop("type") != channel.type:
        raise HTTPException(status_code=400, detail="A channel's type can't be changed")
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Channel name can't be empty")

    async with visibility_change(
            request.app.state, server, exclude_user_id=current_user.id):
        async with channel_layout.locked_server(server.id):
            await channel.refresh_from_db()
            if "group_id" in changes:
                await _check_group(server.id, changes["group_id"])
                if changes["group_id"] == channel.group_id:
                    del changes["group_id"]
                else:
                    changes["position"] = await channel_layout.next_channel_position(
                        server.id, changes["group_id"])
            if changes:
                await channel.update_from_dict(changes)
                await channel.save(update_fields=list(changes))

    channel_response_ = await channel_response(channel)
    if changes:
        # Read-state fields are per user, so they are left out of the frame;
        # clients keep their own values and patch the rest.
        await _broadcast_to_channel(request, channel, {
            "type": "channel_updated",
            "server_id": server.id,
            "channel": channel_response_.model_dump(
                mode="json", exclude=PER_USER_FIELDS),
        }, exclude_user_id=current_user.id)

    return channel_response_


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    channel: Channel = Depends(channel_from_path),
    server: Server = Depends(check_channel(Permission.DELETE_CHANNELS)),
):
    """Delete a channel and all of its messages."""

    await server_queue.idle(server.id)
    viewers = await permissions.viewers(server.id, channel.id)
    was_voice = channel.type == "voice"
    async with channel_layout.locked_server(server.id):
        # Messages cascade at the DB level too, but deleting them explicitly
        # keeps this independent of how the FK was migrated.
        await Message.filter(channel_id=channel.id).delete()
        await channel.delete()

        server = await Server.get(id=server.id)
        server_settings = server.server_settings or {}
        if server_settings.get("default_channel_id") == channel_id:
            del server_settings["default_channel_id"]
            server.server_settings = server_settings
            await server.save(update_fields=["server_settings"])

    permissions.invalidate(server.id)
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_users(viewers, {
            "type": "channel_deleted",
            "server_id": server.id,
            "channel_id": channel_id,
        }, exclude_user_id=current_user.id)

    if was_voice:
        await close_voice_channels(
            getattr(request.app.state, "voice_presence", None), [channel_id])
