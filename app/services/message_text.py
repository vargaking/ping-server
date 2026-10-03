import json

from app.services.message_content import MAX_CONTENT_DEPTH, MAX_CONTENT_NODES

PREVIEW_MAX_CHARS = 140


def _parse_content(raw):
    """Message content as stored or sent: a tiptap dict or a JSON string."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _walk(root):
    """Yield (node, entering) for every dict node under *root*, depth first:
    True on the way in, False after its children. Iterative and bounded, so
    hostile nesting can't exhaust the stack or run for long."""
    stack = [(root, 0, True)]
    seen = 0
    while stack:
        node, depth, entering = stack.pop()
        if not entering:
            yield node, False
            continue
        if not isinstance(node, dict) or seen >= MAX_CONTENT_NODES:
            continue
        seen += 1
        yield node, True
        stack.append((node, depth, False))
        children = node.get("content")
        if isinstance(children, list) and depth < MAX_CONTENT_DEPTH:
            stack.extend((child, depth + 1, True) for child in reversed(children))


def _kind(attachment) -> str:
    return attachment["kind"] if isinstance(attachment, dict) else attachment.kind


def plain_text(content, attachments=()) -> str:
    """One-line preview of a message, at most PREVIEW_MAX_CHARS characters.

    Ported from messagePlainText / messagePreviewText in the frontend.
    """
    parsed = _parse_content(content)
    if isinstance(parsed, dict):
        parts: list[str] = []
        for node, entering in _walk(parsed):
            node_type = node.get("type")
            if not entering:
                # Separate block nodes (paragraphs, list items) with a space.
                if isinstance(node.get("content"), list) and node_type != "doc":
                    parts.append(" ")
                continue
            attrs = node.get("attrs")
            attrs = attrs if isinstance(attrs, dict) else {}
            if node_type == "text":
                parts.append(str(node.get("text") or ""))
            elif node_type == "mention":
                parts.append(f"@{attrs.get('label') or attrs.get('id') or ''}")
            elif node_type == "hardBreak":
                parts.append(" ")
        text = " ".join("".join(parts).split())
    elif parsed is None:
        text = ""
    else:
        text = " ".join(str(parsed).split())

    if not text and attachments:
        if len(attachments) > 1:
            text = f"Sent {len(attachments)} attachments"
        else:
            text = "Sent an image" if _kind(attachments[0]) == "image" else "Sent a file"
    if len(text) > PREVIEW_MAX_CHARS:
        text = text[:PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def mentioned_user_ids(content) -> set[int]:
    """User ids of every mention node in a tiptap document."""
    found: set[int] = set()
    for node, entering in _walk(_parse_content(content)):
        if not entering or node.get("type") != "mention":
            continue
        attrs = node.get("attrs")
        raw = attrs.get("id") if isinstance(attrs, dict) else None
        if isinstance(raw, (int, str)) and not isinstance(raw, bool):
            try:
                found.add(int(raw))
            except ValueError:
                pass
    return found
