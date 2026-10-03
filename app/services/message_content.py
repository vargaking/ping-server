import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# Message content is client-controlled: bound how much of it we read.
MAX_CONTENT_NODES = 10_000
MAX_CONTENT_DEPTH = 100

AttrValue = str | int | bool | None


class InvalidContent(ValueError):
    pass


class Mark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bold", "italic", "strike", "code", "underline", "link"]
    attrs: dict[str, AttrValue] | None = None


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "doc", "paragraph", "text", "hardBreak", "mention", "heading", "bulletList",
        "orderedList", "listItem", "blockquote", "codeBlock", "horizontalRule",
    ]
    attrs: dict[str, AttrValue] | None = None
    text: str | None = None
    marks: list[Mark] | None = None
    content: list["Node"] | None = None

    @model_validator(mode="after")
    def _check_shape(self):
        if self.type == "text":
            if not self.text or self.content is not None:
                raise ValueError("a text node needs text and no content")
        elif self.text is not None or self.marks is not None:
            raise ValueError("only text nodes carry text and marks")
        return self


EMPTY_DOC = {"type": "doc", "content": []}

_VISIBLE_NODE_TYPES = {"text", "mention", "hardBreak"}


def _check_size(raw: dict) -> None:
    stack = [(raw, 1)]
    seen = 0
    while stack:
        node, depth = stack.pop()
        seen += 1
        if seen > MAX_CONTENT_NODES or depth > MAX_CONTENT_DEPTH:
            raise InvalidContent("content is too large")
        children = node.get("content")
        if isinstance(children, list):
            stack.extend((child, depth + 1) for child in children if isinstance(child, dict))


def _has_visible_content(node: dict) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current["type"] in _VISIBLE_NODE_TYPES:
            return True
        stack.extend(current.get("content") or [])
    return False


def _text_doc(text: str) -> dict:
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line
            else {"type": "paragraph"}
            for line in text.split("\n")
        ],
    }


def normalize_content(raw: Any, *, allow_empty: bool) -> dict:
    """Validated TipTap doc for a new or edited message. Raises InvalidContent."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        doc = None
    elif isinstance(raw, str):
        doc = _text_doc(raw)
    elif isinstance(raw, dict):
        doc = raw
    else:
        raise InvalidContent("content must be a document or a string")

    if doc is not None:
        _check_size(doc)
        try:
            node = Node.model_validate(doc)
        except ValidationError as exc:
            raise InvalidContent("content is not a supported document") from exc
        if node.type != "doc":
            raise InvalidContent("content root must be a doc")
        doc = node.model_dump(exclude_none=True)
        if not _has_visible_content(doc):
            doc = None

    if doc is None:
        if not allow_empty:
            raise InvalidContent("message is empty")
        return {"type": "doc", "content": []}
    return doc


def serialize(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
