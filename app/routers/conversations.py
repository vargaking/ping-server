import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.Conversation import Conversation
from ..models.Message import Message
from ..models.User import User
from .channels import (
    HISTORY_PAGE_SIZE,
    MAX_HISTORY_PAGE_SIZE,
    _decode_cursor,
    _encode_cursor,
)
from .users import UserResponse

logger = logging.getLogger("app.routers.conversations")

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    user_id: int


class MessagePreview(BaseModel):
    """Just enough of the last message to render a list-row preview."""
    id: str
    content: str
    user_id: int
    timestamp: datetime
    edited_at: Optional[datetime] = None


class ConversationResponse(BaseModel):
    id: int
    created_at: datetime
    # The participant that isn't the caller — the person the DM is "with".
    other_user: UserResponse
    last_message: Optional[MessagePreview] = None
    # Last activity for client-side sorting: the last message's time, or the
    # conversation's own creation time when it has no messages yet.
    last_activity: datetime


async def _load_participant_conversation(
    conversation_id: int, user: User
) -> Conversation:
    """Fetch a conversation the caller participates in, or 404.

    A non-participant gets a 404, not a 403 — a 403 would confirm the
    conversation exists and leak who is talking to whom.
    """
    conversation = await Conversation.get_or_none(id=conversation_id)
    if not conversation or not conversation.has_participant(user.id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


async def _last_message(conversation_id: int) -> Optional[dict]:
    return await Message.filter(
        conversation_id=conversation_id
    ).order_by("-created_at", "-id").first().values(
        "uuid", "content", "author_id", "timestamp", "edited_at",
    )


def _preview(row: Optional[dict]) -> Optional[MessagePreview]:
    if not row:
        return None
    return MessagePreview(
        id=str(row["uuid"]),
        content=row["content"],
        user_id=row["author_id"],
        timestamp=row["timestamp"],
        edited_at=row["edited_at"],
    )


async def _to_response(conversation: Conversation, user: User) -> ConversationResponse:
    other = await User.get(id=conversation.other_user_id(user.id))
    last_row = await _last_message(conversation.id)
    preview = _preview(last_row)
    last_activity = last_row["timestamp"] if last_row else conversation.created_at
    return ConversationResponse(
        id=conversation.id,
        created_at=conversation.created_at,
        other_user=UserResponse.from_user(other),
        last_message=preview,
        last_activity=last_activity,
    )


@router.post("/", response_model=ConversationResponse, status_code=status.HTTP_200_OK)
async def get_or_create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
):
    """Open the 1:1 conversation with another user, creating it on first use.

    Idempotent: the same pair always resolves to the same conversation, in
    either direction. There is no shared-server restriction — any user may DM
    any other user.
    """
    if body.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot open a conversation with yourself")

    other = await User.get_or_none(id=body.user_id)
    if not other:
        raise HTTPException(status_code=404, detail="User not found")

    user_a_id, user_b_id = Conversation.normalize_pair(current_user.id, other.id)
    conversation, _ = await Conversation.get_or_create(
        user_a_id=user_a_id, user_b_id=user_b_id
    )

    return await _to_response(conversation, current_user)


@router.get("/", response_model=List[ConversationResponse])
async def list_conversations(current_user: User = Depends(get_current_user)):
    """The caller's conversations, most recently active first.

    Each row carries the other participant and a preview of the last message so
    the list can render without a follow-up request per conversation.
    """
    conversations = await Conversation.filter(
        Q(user_a_id=current_user.id) | Q(user_b_id=current_user.id)
    )

    responses = [await _to_response(c, current_user) for c in conversations]
    responses.sort(key=lambda r: r.last_activity, reverse=True)
    return responses


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: int,
    before: Optional[str] = None,
    limit: int = HISTORY_PAGE_SIZE,
    current_user: User = Depends(get_current_user),
):
    """Newest-first page of a conversation's history.

    Same cursor pagination as channel history: pass the previous page's
    next_cursor as before to walk further back.
    """
    limit = max(1, min(limit, MAX_HISTORY_PAGE_SIZE))

    await _load_participant_conversation(conversation_id, current_user)

    query = Message.filter(conversation_id=conversation_id)
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
        "conversation_id",
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


def _serialize(message: dict) -> dict:
    return {
        "id": message["uuid"],
        "content": message["content"],
        "user_id": message["author_id"],
        "conversation_id": message["conversation_id"],
        "timestamp": message["timestamp"],
        "edited_at": message["edited_at"],
    }
