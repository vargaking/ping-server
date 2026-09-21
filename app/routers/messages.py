import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..utils import require_membership

logger = logging.getLogger("app.routers.messages")

router = APIRouter(prefix="/messages", tags=["messages"])


class MessageUpdate(BaseModel):
    # A ProseMirror doc (dict) or a plain string, same shape as an incoming
    # chat frame's content.
    content: Any


def _parse_uuid(message_id: str) -> UUID:
    try:
        return UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")


async def _load_message_and_server(message_id: str) -> tuple[Message, Server]:
    message = await Message.get_or_none(uuid=_parse_uuid(message_id))
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    server = await Server.get_or_none(id=message.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return message, server


def _wire_message(message: Message, content: Any) -> dict:
    """Build a JSON-serialisable message payload for the REST reply and the WS
    frame. content is the raw (un-stringified) form the client sees."""
    edited_at = message.edited_at
    timestamp = message.timestamp
    return {
        "id": str(message.uuid),
        "server_id": message.server_id,
        "channel_id": message.channel_id,
        "user_id": message.author_id,
        "content": content,
        "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
        "edited_at": edited_at.isoformat() if isinstance(edited_at, datetime) else edited_at,
    }


@router.patch("/{message_id}")
async def edit_message(
    message_id: str,
    body: MessageUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Edit a message's content. Only the author may edit; sets edited_at."""
    message, server = await _load_message_and_server(message_id)
    await require_membership(current_user, server)

    if message.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")

    content = body.content
    # Persist dicts as JSON text, matching how ChatService stores incoming frames.
    message.content = json.dumps(content) if isinstance(content, dict) else content
    message.edited_at = datetime.now(timezone.utc)
    await message.save()

    payload = _wire_message(message, content)

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(
            server.id,
            {"type": "message_updated", **payload},
            exclude_user_id=current_user.id,
        )

    return payload


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Delete a message. The author may delete their own; the server owner may
    delete anyone's."""
    message, server = await _load_message_and_server(message_id)
    await require_membership(current_user, server)

    is_author = message.author_id == current_user.id
    is_owner = server.owner_id is not None and server.owner_id == current_user.id
    if not (is_author or is_owner):
        raise HTTPException(status_code=403, detail="Not allowed to delete this message")

    frame = {
        "type": "message_deleted",
        "id": str(message.uuid),
        "server_id": server.id,
        "channel_id": message.channel_id,
    }
    await message.delete()

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(
            server.id, frame, exclude_user_id=current_user.id
        )
