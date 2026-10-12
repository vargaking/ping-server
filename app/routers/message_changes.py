import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..middleware import get_current_user
from ..models.User import User
from ..services.attachments import attachments_by_message
from ..services.message_changes import (
    MAX_LOOKUP,
    PAGE_SIZE,
    InvalidCursor,
    changes_page,
    messages_by_ids,
)
from ..services.reactions import reactions_by_message
from ..services.replies import reply_json, reply_refs
from .messages import _wire_message

# Registered before the messages router so "/messages/changes" is not read as a message id.
router = APIRouter(prefix="/messages", tags=["messages"])


def _content(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@router.get("/changes")
async def get_changes(
    since: str | None = None,
    limit: int = PAGE_SIZE,
    current_user: User = Depends(get_current_user),
):
    """Which messages were edited or deleted since *since*, by id and kind. The
    cursor in the answer is where to resume; without *since* it is the only
    thing returned."""
    try:
        return await changes_page(current_user.id, since, min(max(limit, 1), PAGE_SIZE))
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="Invalid cursor")


@router.get("")
async def get_messages_by_ids(
    ids: list[UUID] = Query(default=[], max_length=MAX_LOOKUP),
    current_user: User = Depends(get_current_user),
):
    """The messages among *ids* the caller can view, as in a message_updated
    frame. An id that is missing from the answer is gone or not visible."""
    messages = await messages_by_ids(current_user.id, list(dict.fromkeys(ids)))
    message_ids = [message.id for message in messages]
    attachments = await attachments_by_message(message_ids)
    reactions = await reactions_by_message(message_ids)
    refs = await reply_refs(message.reply_to_uuid for message in messages)
    return {"messages": [
        _wire_message(
            message, _content(message.content), attachments.get(message.id, []),
            reactions.get(message.id, []), reply_json(message.reply_to_uuid, refs))
        for message in messages
    ]}
