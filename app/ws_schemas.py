"""Pydantic models for the WebSocket wire protocol.

Every inbound frame is validated against a discriminated union on ``type``
before it is dispatched. A frame that does not match is rejected with an
``error`` frame instead of raising out of the socket loop and killing the
connection. Unknown fields are ignored (the server never trusts client-supplied
identity anyway — see ChatService), but the known fields must be well-typed.
"""
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

__all__ = [
    "ConnectionInitFrame",
    "DisconnectFrame",
    "MessageFrame",
    "WSFrame",
    "parse_frame",
    "ValidationError",
]


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


WSFrame = Annotated[
    Union[ConnectionInitFrame, DisconnectFrame, MessageFrame],
    Field(discriminator="type"),
]

_frame_adapter: TypeAdapter = TypeAdapter(WSFrame)


def parse_frame(raw: Any):
    """Validate a raw decoded frame into one of the WSFrame models.

    Raises ``pydantic.ValidationError`` on anything that is not a recognised,
    well-formed frame.
    """
    return _frame_adapter.validate_python(raw)
