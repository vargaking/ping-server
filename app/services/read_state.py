import logging
from collections.abc import Collection, Mapping

from pypika_tortoise import Order
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Expression, ResolveContext, ResolveResult
from tortoise.functions import Max

from app.models.Message import Message
from app.models.ReadState import ReadState
from app.services.push import push

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
    this is a no-op. Returns whether the marker actually moved. A move that
    covers a message we sent a push for also retracts that push.
    """
    moved = await _advance(
        user_id, channel_id=channel_id, conversation_id=conversation_id,
        message_pk=message_pk)
    if moved:
        tag = f"ch-{channel_id}" if channel_id is not None else f"dm-{conversation_id}"
        push.notify_read(user_id, tag, message_pk)
    return moved


async def _advance(
    user_id: int,
    *,
    channel_id: int | None = None,
    conversation_id: int | None = None,
    message_pk: int,
) -> bool:
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


async def _last_messages(thread_field: str, thread_ids: list[int]) -> dict[int, dict]:
    """{thread_id: {"id", "uuid"}} of the newest message in each thread, via one
    grouped MAX(id) plus one lookup, instead of scanning every message."""
    newest = await Message.filter(**{f"{thread_field}__in": thread_ids}).annotate(
        max_id=Max("id")).group_by(thread_field).values(thread_field, "max_id")
    rows = await Message.filter(
        id__in=[n["max_id"] for n in newest]).values(thread_field, "id", "uuid")
    return {row[thread_field]: row for row in rows}


async def _resolve_markers(
    thread_field: str, thread_ids: list[int], markers: dict, last_by_thread: dict
) -> dict[int, str | None]:
    result = {}
    for tid in thread_ids:
        marker = markers.get(tid)
        last_row = last_by_thread.get(tid)
        if marker is None:
            result[tid] = None
        elif last_row is not None and last_row["id"] <= marker:
            # Fully read, the common case: no extra query.
            result[tid] = str(last_row["uuid"])
        else:
            result[tid] = await resolve_marker_uuid(marker, **{thread_field: tid})
    return result


class _NewestReadUuid(Expression):
    """For a read state row, the uuid of the newest message in its channel with
    id <= its marker, as a subquery correlated to the row."""

    def resolve(self, resolve_context: ResolveContext) -> ResolveResult:
        read_state, message = resolve_context.table, Message._meta.basetable
        newest = (
            message.select(message.uuid)
            .where(message.channel_id == read_state.channel_id)
            .where(message.id <= read_state.last_read_message_id)
            .orderby(message.id, order=Order.desc)
            .limit(1))
        return ResolveResult(term=newest)


async def _newest_read_uuids(
    pairs: Collection[tuple[int, int]], markers: Mapping[tuple[int, int], int]
) -> dict[tuple[int, int], str | None]:
    """{(user_id, channel_id): what resolve_marker_uuid gives} for *pairs*, in one query."""
    rows = await ReadState.filter(
        user_id__in={user_id for user_id, _ in pairs},
        channel_id__in={channel_id for _, channel_id in pairs},
        last_read_message_id__in={markers[pair] for pair in pairs},
    ).annotate(newest=_NewestReadUuid()).values_list("user_id", "channel_id", "newest")
    return {(user_id, channel_id): None if newest is None else str(newest)
            for user_id, channel_id, newest in rows if (user_id, channel_id) in pairs}


async def batch_channel_states(
    wanted: Mapping[int, Collection[int]]
) -> dict[int, dict[int, dict]]:
    """batch_channel_state for many users at once, from the same few queries
    however many users and channels.

    Returns {user_id: {channel_id: {"last_read_message_id": str|None,
    "last_message_id": str|None}}}.
    """
    channel_ids = sorted({channel_id for ids in wanted.values() for channel_id in ids})
    if not channel_ids:
        return {}

    markers = {
        (user_id, channel_id): marker
        for user_id, channel_id, marker in await ReadState.filter(
            user_id__in=list(wanted), channel_id__in=channel_ids
        ).values_list("user_id", "channel_id", "last_read_message_id")
        if channel_id in wanted[user_id]
    }
    last_by_channel = await _last_messages("channel_id", channel_ids)
    behind = {
        marker for (_, channel_id), marker in markers.items()
        if channel_id in last_by_channel and marker < last_by_channel[channel_id]["id"]}
    marked = {row["id"]: row for row in await Message.filter(id__in=list(behind)).values(
        "id", "channel_id", "uuid")} if behind else {}

    deleted = {
        pair for pair, marker in markers.items()
        if pair[1] in last_by_channel and marker < last_by_channel[pair[1]]["id"]
        and marked.get(marker, {}).get("channel_id") != pair[1]}
    fallback = await _newest_read_uuids(deleted, markers) if deleted else {}

    result: dict[int, dict[int, dict]] = {}
    for user_id, ids in wanted.items():
        states = result[user_id] = {}
        for channel_id in ids:
            marker = markers.get((user_id, channel_id))
            last = last_by_channel.get(channel_id)
            if marker is None or last is None:
                last_read = None
            elif marker >= last["id"]:
                last_read = str(last["uuid"])
            elif marked.get(marker, {}).get("channel_id") == channel_id:
                last_read = str(marked[marker]["uuid"])
            else:
                last_read = fallback.get((user_id, channel_id))
            states[channel_id] = {
                "last_read_message_id": last_read,
                "last_message_id": str(last["uuid"]) if last else None,
            }
    return result


async def batch_channel_state(user_id: int, channel_ids: list[int]) -> dict:
    """Read marker + last message per channel for a channel list.

    Returns {channel_id: {"last_read_message_id": str|None, "last_message_id": str|None}}.
    """
    return (await batch_channel_states({user_id: channel_ids})).get(user_id, {})


async def batch_conversation_state(user_id: int, conversation_ids: list[int]) -> dict:
    """Read marker + last message + unread count per conversation.

    Returns {conversation_id: {"last_read_message_id": str|None,
    "last_message_id": str|None, "unread_count": int}}.
    """
    if not conversation_ids:
        return {}

    markers = dict(await ReadState.filter(
        user_id=user_id, conversation_id__in=conversation_ids
    ).values_list("conversation_id", "last_read_message_id"))
    last_by_convo = await _last_messages("conversation_id", conversation_ids)
    last_read = await _resolve_markers(
        "conversation_id", conversation_ids, markers, last_by_convo)

    result = {}
    for cid in conversation_ids:
        marker = markers.get(cid)
        last_row = last_by_convo.get(cid)
        if last_row is None or (marker is not None and last_row["id"] <= marker):
            unread_count = 0
        else:
            unread_query = Message.filter(conversation_id=cid).exclude(author_id=user_id)
            if marker is not None:
                unread_query = unread_query.filter(id__gt=marker)
            unread_count = await unread_query.count()

        result[cid] = {
            "last_read_message_id": last_read[cid],
            "last_message_id": str(last_row["uuid"]) if last_row else None,
            "unread_count": unread_count,
        }
    return result
