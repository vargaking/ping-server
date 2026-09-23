import logging

from tortoise.exceptions import IntegrityError

from app.models.Message import Message
from app.models.ReadState import ReadState

logger = logging.getLogger("app.services.read_state")


async def advance(
    user_id: int,
    *,
    channel_id: int | None = None,
    conversation_id: int | None = None,
    message_pk: int,
) -> bool:
    """Move *user_id*'s read marker for a channel or conversation forward to
    *message_pk* (an internal Message.id, not a uuid).

    Never moves it backward: if the stored marker is already >= message_pk,
    this is a no-op. Returns whether the marker actually moved.
    """
    assert (channel_id is None) != (conversation_id is None), \
        "exactly one of channel_id/conversation_id must be set"

    row = await ReadState.get_or_none(
        user_id=user_id, channel_id=channel_id, conversation_id=conversation_id)
    if row is None:
        try:
            row = await ReadState.create(
                user_id=user_id, channel_id=channel_id,
                conversation_id=conversation_id,
                last_read_message_id=message_pk,
            )
            return True
        except IntegrityError:
            # Lost a create race to a concurrent request; fall through and
            # advance the row it created instead.
            row = await ReadState.get(
                user_id=user_id, channel_id=channel_id,
                conversation_id=conversation_id)

    # Conditional update keyed on the row still being behind message_pk, so
    # two concurrent "mark as read" calls can never race the marker backward.
    updated = await ReadState.filter(
        pk=row.pk, last_read_message_id__lt=message_pk
    ).update(last_read_message_id=message_pk)
    return bool(updated)


async def get_marker(
    user_id: int, *, channel_id: int | None = None, conversation_id: int | None = None
) -> int | None:
    row = await ReadState.get_or_none(
        user_id=user_id, channel_id=channel_id, conversation_id=conversation_id)
    return row.last_read_message_id if row else None


async def resolve_marker_uuid(
    marker: int | None, *, channel_id: int | None = None, conversation_id: int | None = None
) -> str | None:
    """The uuid of the newest message with id <= marker in this thread.

    Falls back past a marker message that has since been deleted, to the
    previous surviving message. None if there is no such message (never read,
    or every message up to the marker is gone).
    """
    if marker is None:
        return None
    query = Message.filter(id__lte=marker)
    query = query.filter(channel_id=channel_id) if channel_id is not None \
        else query.filter(conversation_id=conversation_id)
    row = await query.order_by("-id").first().values("uuid")
    return str(row["uuid"]) if row else None


async def last_message_uuid(
    *, channel_id: int | None = None, conversation_id: int | None = None
) -> str | None:
    query = Message.filter(channel_id=channel_id) if channel_id is not None \
        else Message.filter(conversation_id=conversation_id)
    row = await query.order_by("-id").first().values("uuid")
    return str(row["uuid"]) if row else None


async def batch_channel_state(user_id: int, channel_ids: list[int]) -> dict:
    """Read markers + last message per channel, in a handful of queries
    instead of one pair per channel.

    Returns {channel_id: {"last_read_message_id": str|None, "last_message_id": str|None}}.
    """
    if not channel_ids:
        return {}

    markers = await ReadState.filter(
        user_id=user_id, channel_id__in=channel_ids
    ).values("channel_id", "last_read_message_id")
    marker_by_channel = {m["channel_id"]: m["last_read_message_id"] for m in markers}

    # One query for the last message per channel: fetch every message's
    # (channel_id, id) pair for these channels in id order and keep the last
    # one seen per channel. channel_ids is small (a server's channel list),
    # so this stays cheap.
    last_rows = await Message.filter(channel_id__in=channel_ids).order_by("id").values(
        "channel_id", "id", "uuid")
    last_by_channel: dict[int, dict] = {}
    for row in last_rows:
        last_by_channel[row["channel_id"]] = row

    # Resolve each marker to the newest surviving message at-or-before it.
    # This still needs one lookup per channel that has a marker; markers are
    # rare enough (one per user per channel) that this is fine.
    result = {}
    for cid in channel_ids:
        marker = marker_by_channel.get(cid)
        last_row = last_by_channel.get(cid)
        if marker is not None and last_row is not None and last_row["id"] <= marker:
            # The stored marker is at or past the last message (the common
            # "fully read" case) — no extra query needed.
            last_read_uuid = str(last_row["uuid"])
        elif marker is not None:
            last_read_uuid = await resolve_marker_uuid(marker, channel_id=cid)
        else:
            last_read_uuid = None
        result[cid] = {
            "last_read_message_id": last_read_uuid,
            "last_message_id": str(last_row["uuid"]) if last_row else None,
        }
    return result


async def batch_conversation_state(user_id: int, conversation_ids: list[int]) -> dict:
    """Read markers + last message + unread count per conversation.

    Returns {conversation_id: {"last_read_message_id": str|None,
    "last_message_id": str|None, "unread_count": int}}.
    """
    if not conversation_ids:
        return {}

    markers = await ReadState.filter(
        user_id=user_id, conversation_id__in=conversation_ids
    ).values("conversation_id", "last_read_message_id")
    marker_by_convo = {m["conversation_id"]: m["last_read_message_id"] for m in markers}

    last_rows = await Message.filter(conversation_id__in=conversation_ids).order_by("id").values(
        "conversation_id", "id", "uuid")
    last_by_convo: dict[int, dict] = {}
    for row in last_rows:
        last_by_convo[row["conversation_id"]] = row

    result = {}
    for cid in conversation_ids:
        marker = marker_by_convo.get(cid)
        last_row = last_by_convo.get(cid)
        if marker is not None and last_row is not None and last_row["id"] <= marker:
            last_read_uuid = str(last_row["uuid"])
        elif marker is not None:
            last_read_uuid = await resolve_marker_uuid(marker, conversation_id=cid)
        else:
            last_read_uuid = None

        unread_query = Message.filter(conversation_id=cid).exclude(author_id=user_id)
        if marker is not None:
            unread_query = unread_query.filter(id__gt=marker)
        unread_count = await unread_query.count()

        result[cid] = {
            "last_read_message_id": last_read_uuid,
            "last_message_id": str(last_row["uuid"]) if last_row else None,
            "unread_count": unread_count,
        }
    return result
