from typing import Iterable
from uuid import UUID

from ..models.Message import Message
from .attachments import attachments_by_message
from .message_text import plain_text


async def reply_refs(uuids: Iterable[UUID | None]) -> dict[UUID, dict]:
    """Quote payload for each original, in one query plus one for attachments.
    An original that no longer exists maps to a deleted marker."""
    wanted = {u for u in uuids if u is not None}
    if not wanted:
        return {}
    rows = await Message.filter(uuid__in=wanted).values(
        "id", "uuid", "author_id", "content")
    attachments = await attachments_by_message([row["id"] for row in rows])
    refs = {
        row["uuid"]: {
            "id": str(row["uuid"]),
            "user_id": row["author_id"],
            "preview": plain_text(row["content"], attachments.get(row["id"], [])),
        }
        for row in rows
    }
    return {u: refs.get(u) or {"id": str(u), "deleted": True} for u in wanted}


def reply_json(reply_to_uuid: UUID | None, refs: dict[UUID, dict]) -> dict | None:
    if reply_to_uuid is None:
        return None
    return refs[reply_to_uuid]
