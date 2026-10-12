"""The feed of edited and deleted messages, and the lookup of messages by id.

A device that was away asks what changed since its cursor and then fetches the
edited messages by id. The feed carries ids and kinds only, never content.

Edits are read from ``messages.edited_at`` and deletes from the tombstone table,
merged by ``(time, kind, row id)`` with edits first. A cursor is either a walk in
progress (the exact key of the last row sent) or a point in time to resume from.
"""
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from tortoise import BaseDBAsyncClient
from tortoise.expressions import Q

from ..models.Conversation import Conversation
from ..models.Message import Message
from ..models.MessageTombstone import MessageTombstone
from ..models.Server import Server
from .permissions import permissions

RETENTION = timedelta(days=30)
OVERLAP = timedelta(seconds=30)
PAGE_SIZE = 500
MAX_LOOKUP = 100

BEFORE_ROWS = -1
EDITED = 0
DELETED = 1
KINDS = {EDITED: "edited", DELETED: "deleted"}

CURSOR_VERSION = 1


class InvalidCursor(Exception):
    pass


@dataclass(frozen=True)
class FeedPosition:
    at: datetime
    kind: int
    row_id: int
    # Set only while a walk has more pages: the point the walk ends at.
    floor: datetime | None = None


def encode(position: FeedPosition) -> str:
    raw = json.dumps(
        {
            "v": CURSOR_VERSION,
            "t": position.at.isoformat(),
            "k": position.kind,
            "i": position.row_id,
            "f": position.floor.isoformat() if position.floor else None,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


# Row ids are 32-bit integer columns; a larger id would fail in the query.
MAX_ROW_ID = 2**31 - 1


def decode(cursor: str) -> FeedPosition | None:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        row_id = data["i"]
        if (data["v"] != CURSOR_VERSION or data["k"] not in (BEFORE_ROWS, EDITED, DELETED)
                or not isinstance(row_id, int) or isinstance(row_id, bool)
                or not 0 <= row_id <= MAX_ROW_ID):
            return None
        floor = data["f"]
        return FeedPosition(
            _parse_time(data["t"]), data["k"], row_id,
            _parse_time(floor) if floor is not None else None)
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


def _after(field: str, kind: int, position: FeedPosition) -> Q:
    """Rows of *kind* that sort strictly after *position*."""
    if position.kind < kind:
        return Q(**{f"{field}__gte": position.at})
    if position.kind == kind:
        return Q(**{f"{field}__gt": position.at}) | Q(
            **{field: position.at, "id__gt": position.row_id})
    return Q(**{f"{field}__gt": position.at})


async def readable_scope(user_id: int) -> tuple[set[int], set[int]]:
    """The channel ids the user can view and the conversation ids they are in."""
    channel_ids: set[int] = set()
    for server in await Server.filter(server_users__user_id=user_id):
        view = await permissions.view(user_id, server)
        if view is not None:
            channel_ids |= view.visible_channels()
    conversation_ids = set(await Conversation.filter(
        Q(user_a_id=user_id) | Q(user_b_id=user_id)).values_list("id", flat=True))
    return channel_ids, conversation_ids


def _scope_filter(channel_ids: set[int], conversation_ids: set[int]) -> Q:
    return Q(channel_id__in=list(channel_ids)) | Q(conversation_id__in=list(conversation_ids))


async def changes_page(user_id: int, since: str | None, limit: int) -> dict:
    now = datetime.now(timezone.utc)
    fresh = encode(FeedPosition(now - OVERLAP, BEFORE_ROWS, 0))
    if since is None:
        return {"reset": False, "changes": [], "cursor": fresh, "has_more": False}
    position = decode(since)
    if position is None:
        raise InvalidCursor
    if position.at < now - RETENTION:
        return {"reset": True, "changes": [], "cursor": fresh, "has_more": False}

    floor = position.floor or now - OVERLAP
    scope = _scope_filter(*await readable_scope(user_id))
    edits = await Message.filter(scope, _after("edited_at", EDITED, position)) \
        .order_by("edited_at", "id").limit(limit + 1) \
        .values_list("edited_at", "id", "uuid")
    deletes = await MessageTombstone.filter(scope, _after("deleted_at", DELETED, position)) \
        .order_by("deleted_at", "id").limit(limit + 1) \
        .values_list("deleted_at", "id", "message_uuid")
    rows = sorted(
        [(at, EDITED, row_id, uuid) for at, row_id, uuid in edits]
        + [(at, DELETED, row_id, uuid) for at, row_id, uuid in deletes])
    has_more = len(rows) > limit
    rows = rows[:limit]
    if has_more:
        at, kind, row_id, _ = rows[-1]
        cursor = encode(FeedPosition(at, kind, row_id, floor))
    else:
        cursor = encode(FeedPosition(floor, BEFORE_ROWS, 0))
    return {
        "reset": False,
        "changes": [
            {"id": str(uuid), "kind": KINDS[kind], "at": at.isoformat()}
            for at, kind, _, uuid in rows
        ],
        "cursor": cursor,
        "has_more": has_more,
    }


async def messages_by_ids(user_id: int, ids: list[UUID]) -> list[Message]:
    """The messages among *ids* that the user can view, in no particular order."""
    if not ids:
        return []
    scope = _scope_filter(*await readable_scope(user_id))
    return await Message.filter(scope, uuid__in=ids)


async def write_tombstone(message: Message) -> None:
    await MessageTombstone.create(
        message_uuid=message.uuid,
        server_id=message.server_id,
        channel_id=message.channel_id,
        conversation_id=message.conversation_id,
        deleted_at=datetime.now(timezone.utc),
    )


async def tombstone_channel_messages_of(conn: BaseDBAsyncClient, user_id: int) -> None:
    """Deleting an account cascades its channel messages away. Its DMs need no
    tombstones: their conversations go too, and the tombstones with them."""
    first, second = ("?", "?") if conn.capabilities.dialect == "sqlite" else ("$1", "$2")
    await conn.execute_query(
        "INSERT INTO message_tombstones (message_uuid, server_id, channel_id, conversation_id, deleted_at) "
        f"SELECT uuid, server_id, channel_id, NULL, {first} FROM messages "
        f"WHERE author_id = {second} AND conversation_id IS NULL",
        [datetime.now(timezone.utc), user_id],
    )


async def prune_tombstones() -> int:
    return await MessageTombstone.filter(
        deleted_at__lt=datetime.now(timezone.utc) - RETENTION).delete()
