import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.Attachment import Attachment
from ..models.Conversation import Conversation
from ..models.ForumPost import ForumPost
from ..models.Message import Message
from ..models.Reaction import Reaction
from ..models.Server import Server
from ..models.User import User
from ..permissions import Permission, require_permission
from ..services import forum
from ..services.attachments import attachments_by_message, delete_file
from ..services.message_content import InvalidContent, normalize_content, serialize
from ..services.permissions import permissions
from ..services.reactions import (
    MAX_DISTINCT_EMOJIS_PER_MESSAGE,
    normalize_emoji,
    reactions_by_message,
)
from ..services.replies import reply_json, reply_refs

logger = logging.getLogger("app.routers.messages")

router = APIRouter(prefix="/messages", tags=["messages"])


class MessageUpdate(BaseModel):
    # A ProseMirror doc (dict) or a plain string; validated by normalize_content.
    content: Any


def _parse_uuid(message_id: str) -> UUID:
    try:
        return UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")


async def _load_message_and_scope(
    message_id: str, user: User
) -> tuple[Message, Server | None, Conversation | None]:
    """Fetch a message the caller can see, plus the server or conversation it
    lives in (exactly one of the two is set).

    DM messages are only visible to the two participants; anyone else gets a
    404, matching the conversation endpoints, so ids can't be probed.
    """
    message = await Message.get_or_none(uuid=_parse_uuid(message_id))
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.conversation_id is not None:
        conversation = await Conversation.get_or_none(id=message.conversation_id)
        if not conversation or not conversation.has_participant(user.id):
            raise HTTPException(status_code=404, detail="Message not found")
        return message, None, conversation

    server = await Server.get_or_none(id=message.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await require_permission(user, server, Permission(0))
    return message, server, None


async def _notify(
    request: Request,
    server: Server | None,
    conversation: Conversation | None,
    frame: dict,
    exclude_user_id: int | None,
) -> None:
    """Fan *frame* out to the server's members or the DM's participants."""
    comms = getattr(request.app.state, "comms", None)
    if comms is None:
        return
    if conversation is not None:
        await comms.send_to_users(
            [conversation.user_a_id, conversation.user_b_id],
            frame, exclude_user_id=exclude_user_id,
        )
    else:
        await comms.broadcast_to_server(server.id, frame, exclude_user_id=exclude_user_id)


def _wire_message(
    message: Message,
    content: Any,
    attachments: list[dict],
    reactions: list[dict],
    reply_to: dict | None,
) -> dict:
    """Build a JSON-serialisable message payload for the REST reply and the WS
    frame. content is the raw (un-stringified) form the client sees."""
    edited_at = message.edited_at
    timestamp = message.timestamp
    return {
        "id": str(message.uuid),
        "server_id": message.server_id,
        "channel_id": message.channel_id,
        "post_id": message.post_id,
        "conversation_id": message.conversation_id,
        "user_id": message.author_id,
        "content": content,
        "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
        "edited_at": edited_at.isoformat() if isinstance(edited_at, datetime) else edited_at,
        "attachments": attachments,
        "reactions": reactions,
        "reply_to": reply_to,
        "embeds": (message.metadata or {}).get("embeds", []),
    }


@router.patch("/{message_id}")
async def edit_message(
    message_id: str,
    body: MessageUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Edit a message's content. Only the author may edit; sets edited_at."""
    message, server, conversation = await _load_message_and_scope(
        message_id, current_user)

    if message.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")

    attachments = (await attachments_by_message([message.id])).get(message.id, [])
    try:
        content = normalize_content(body.content, allow_empty=bool(attachments))
    except InvalidContent:
        raise HTTPException(status_code=422, detail="Invalid message content")
    message.content = serialize(content)
    message.edited_at = datetime.now(timezone.utc)
    await message.save()

    reactions = (await reactions_by_message([message.id])).get(message.id, [])
    refs = await reply_refs([message.reply_to_uuid])
    payload = _wire_message(
        message, content, attachments, reactions,
        reply_json(message.reply_to_uuid, refs))
    await _notify(
        request, server, conversation,
        {"type": "message_updated", **payload}, current_user.id,
    )

    return payload


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Delete a message. The author may delete their own; in a server channel
    anyone with MANAGE_MESSAGES may delete anyone's. DMs have no moderators,
    so author only."""
    message, server, conversation = await _load_message_and_scope(
        message_id, current_user)

    is_author = message.author_id == current_user.id
    is_moderator = server is not None and await permissions.has(
        current_user.id, server, Permission.MANAGE_MESSAGES)
    if not (is_author or is_moderator):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")

    frame = {
        "type": "message_deleted",
        "id": str(message.uuid),
        "server_id": message.server_id,
        "channel_id": message.channel_id,
        "post_id": message.post_id,
        "conversation_id": message.conversation_id,
    }
    post = None
    if message.post_id is not None:
        post = await ForumPost.get(id=message.post_id)
        if post.opening_message_id == message.id:
            raise HTTPException(status_code=409, detail="Delete the post instead")
    storage_paths = await Attachment.filter(
        message_id=message.id).values_list("storage_path", flat=True)
    async with in_transaction():
        await message.delete()
        if post is not None:
            await ForumPost.filter(id=post.id, reply_count__gt=0).update(
                reply_count=F("reply_count") - 1)
    for storage_path in storage_paths:
        delete_file(storage_path)

    await _notify(request, server, conversation, frame, current_user.id)
    if post is not None:
        await post.refresh_from_db()
        await _notify(
            request, server, None,
            forum.updated_frame(server.id, (await forum.rows([post]))[0]), None)


async def _load_reactable_message(
    message_id: str, emoji: str, user: User
) -> tuple[Message, Server | None, Conversation | None, str]:
    message, server, conversation = await _load_message_and_scope(message_id, user)
    if server is not None:
        await require_permission(user, server, Permission.VIEW_CHANNEL)
    normalized = normalize_emoji(emoji)
    if normalized is None:
        raise HTTPException(status_code=400, detail="Invalid emoji")
    return message, server, conversation, normalized


def _reaction_frame(kind: str, message: Message, emoji: str, user_id: int) -> dict:
    return {
        "type": kind,
        "message_id": str(message.uuid),
        "server_id": message.server_id,
        "channel_id": message.channel_id,
        "conversation_id": message.conversation_id,
        "emoji": emoji,
        "user_id": user_id,
    }


@router.put("/{message_id}/reactions/{emoji}", status_code=status.HTTP_204_NO_CONTENT)
async def add_reaction(
    message_id: str,
    emoji: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """React to a message. Reacting twice with the same emoji is a no-op."""
    message, server, conversation, emoji = await _load_reactable_message(
        message_id, emoji, current_user)

    already_reacted = await Reaction.filter(
        message_id=message.id, user_id=current_user.id, emoji=emoji).exists()
    if already_reacted:
        return

    emoji_taken = await Reaction.filter(message_id=message.id, emoji=emoji).exists()
    if not emoji_taken:
        distinct = await Reaction.filter(message_id=message.id).distinct().values_list(
            "emoji", flat=True)
        if len(distinct) >= MAX_DISTINCT_EMOJIS_PER_MESSAGE:
            raise HTTPException(status_code=400, detail="Too many reactions")

    try:
        await Reaction.create(
            message_id=message.id, user_id=current_user.id, emoji=emoji)
    except IntegrityError:
        # A concurrent identical request won the race.
        return

    await _notify(
        request, server, conversation,
        _reaction_frame("reaction_added", message, emoji, current_user.id), None,
    )


@router.delete("/{message_id}/reactions/{emoji}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_reaction(
    message_id: str,
    emoji: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Remove your own reaction. Removing one you never added is a no-op."""
    message, server, conversation, emoji = await _load_reactable_message(
        message_id, emoji, current_user)

    deleted = await Reaction.filter(
        message_id=message.id, user_id=current_user.id, emoji=emoji).delete()
    if not deleted:
        return

    await _notify(
        request, server, conversation,
        _reaction_frame("reaction_removed", message, emoji, current_user.id), None,
    )
