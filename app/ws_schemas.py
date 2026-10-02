"""Pydantic models for the WebSocket wire protocol.

Every inbound frame is validated against a discriminated union on ``type``
before it is dispatched. A frame that does not match is rejected with an
``error`` frame instead of raising out of the socket loop and killing the
connection. Unknown fields are ignored (the server never trusts client-supplied
identity anyway — see ChatService), but the known fields must be well-typed.
"""
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.services.attachments import MAX_ATTACHMENTS_PER_MESSAGE

__all__ = [
    "MAX_FRAME_BYTES",
    "FrameDecodeError",
    "decode_frame",
    "ConnectionInitFrame",
    "DisconnectFrame",
    "MessageFrame",
    "DirectMessageFrame",
    "ActivityFrame",
    "PingFrame",
    "WSFrame",
    "parse_frame",
    "ValidationError",
]


# Message content is unbounded tiptap JSON and attachments travel over HTTP,
# so this is the only ceiling on what one frame can carry.
MAX_FRAME_BYTES = 256 * 1024


class FrameDecodeError(ValueError):
    """The raw socket message could not be turned into a JSON value."""


def decode_frame(message: dict) -> Any:
    """Decode an ASGI ``websocket.receive`` message into a JSON value.

    Raises ``FrameDecodeError`` for binary, oversize or unparseable input.
    """
    text = message.get("text")
    if text is None:
        raise FrameDecodeError("binary frame")
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise FrameDecodeError(f"frame over {MAX_FRAME_BYTES} bytes")
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise FrameDecodeError(f"not valid JSON ({type(exc).__name__})") from exc


class ConnectionInitFrame(BaseModel):
    type: Literal["connection_init"]


class DisconnectFrame(BaseModel):
    type: Literal["disconnect"]


class MessageFrame(BaseModel):
    type: Literal["message"]
    id: str
    server_id: int
    channel_id: int
    # A ProseMirror doc (dict) or plain string; validated for presence only.
    content: Any
    timestamp: str
    metadata: dict = Field(default_factory=dict)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)
    reply_to: str | None = None


class DirectMessageFrame(BaseModel):
    type: Literal["direct_message"]
    id: str
    conversation_id: int
    # A ProseMirror doc (dict) or plain string; validated for presence only.
    content: Any
    timestamp: str
    metadata: dict = Field(default_factory=dict)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)
    reply_to: str | None = None


class ActivityFrame(BaseModel):
    type: Literal["activity"]
    state: Literal["active", "idle"]


class PingFrame(BaseModel):
    type: Literal["ping"]
    t: float = Field(allow_inf_nan=False)


WSFrame = Annotated[
    Union[
        ConnectionInitFrame, DisconnectFrame, MessageFrame, DirectMessageFrame,
        ActivityFrame, PingFrame,
    ],
    Field(discriminator="type"),
]

_frame_adapter: TypeAdapter = TypeAdapter(WSFrame)


def parse_frame(raw: Any):
    """Validate a raw decoded frame into one of the WSFrame models.

    Raises ``pydantic.ValidationError`` on anything that is not a recognised,
    well-formed frame.
    """
    return _frame_adapter.validate_python(raw)
