import logging
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import anyio
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from ..models.Attachment import Attachment
from ..models.Channel import Channel
from ..models.Conversation import Conversation
from ..models.Server import Server
from ..permissions import Permission, require_permission

logger = logging.getLogger("app.services.attachments")

_DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 10

_IMAGE_CONTENT_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def attachments_root() -> Path:
    """Read per call so a test can point it at a temp dir via env."""
    root = Path(os.getenv("ATTACHMENTS_ROOT", "attachments")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_attachment_bytes() -> int:
    try:
        return int(os.getenv("MAX_ATTACHMENT_BYTES", _DEFAULT_MAX_ATTACHMENT_BYTES))
    except ValueError:
        return _DEFAULT_MAX_ATTACHMENT_BYTES


def sanitize_filename(name: str) -> str:
    """The display/download name for an upload: no directories, no control
    characters, single spaces, at most 255 characters."""
    name = re.split(r"[/\\]", name or "")[-1]
    name = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in name)
    name = " ".join(name.split())[:255].strip()
    if name in ("", ".", ".."):
        return "file"
    return name


def sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """Return (content_type, width, height) if *data* really is a PNG, JPEG,
    GIF or WEBP image, else None. The client-declared type is never used."""
    try:
        with Image.open(BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in _IMAGE_CONTENT_TYPES:
            return None
        # verify() leaves the image unusable, so reopen for the size.
        with Image.open(BytesIO(data)) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError):
        return None
    return _IMAGE_CONTENT_TYPES[fmt], width, height


def _resolve_inside_root(rel_path: str) -> Path | None:
    root = attachments_root()
    path = (root / rel_path).resolve()
    return path if path.is_relative_to(root) else None


async def store(data: bytes, subdir: str) -> str:
    """Write *data* under ATTACHMENTS_ROOT/<subdir> and return its path relative
    to the root. The file name is random and carries no user-supplied part."""
    root = attachments_root()
    abs_dir = (root / subdir).resolve()
    if not abs_dir.is_relative_to(root):
        raise ValueError(f"Attachment directory outside ATTACHMENTS_ROOT: {subdir}")
    abs_dir.mkdir(parents=True, exist_ok=True)
    abs_path = abs_dir / uuid.uuid4().hex
    await anyio.to_thread.run_sync(abs_path.write_bytes, data)
    return abs_path.relative_to(root).as_posix()


def delete_file(rel_path: str) -> None:
    path = _resolve_inside_root(rel_path)
    if path is None:
        logger.error("Refusing to delete outside ATTACHMENTS_ROOT: %s", rel_path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete attachment file %s", rel_path, exc_info=True)


async def attachments_by_message(message_ids: list[int]) -> dict[int, list[dict]]:
    """Wire form of the attachments of each message, in one query. Messages
    without attachments are absent from the result."""
    grouped: dict[int, list[dict]] = {}
    if not message_ids:
        return grouped
    rows = await Attachment.filter(
        message_id__in=message_ids).order_by("created_at", "id")
    for attachment in rows:
        grouped.setdefault(attachment.message_id, []).append(attachment.to_json())
    return grouped


def _list_files(root: Path) -> list[Path]:
    return [
        Path(dirpath) / name
        for dirpath, _, names in os.walk(root)
        for name in names
    ]


async def prune_attachments(max_age: timedelta = timedelta(hours=24)) -> dict[str, int]:
    """Delete uploads that never got a message, and files no row points to.

    Files newer than *max_age* are left alone even without a row, since an
    upload writes its file just before it inserts the row.
    """
    cutoff = datetime.now(timezone.utc) - max_age
    expired = await Attachment.filter(message_id=None, created_at__lt=cutoff)
    for attachment in expired:
        await attachment.delete()
        delete_file(attachment.storage_path)

    root = attachments_root()
    referenced = set(await Attachment.all().values_list("storage_path", flat=True))
    files = await anyio.to_thread.run_sync(_list_files, root)
    file_cutoff = time.time() - max_age.total_seconds()
    orphaned = 0
    for path in files:
        if path.relative_to(root).as_posix() in referenced:
            continue
        try:
            if path.stat().st_mtime >= file_cutoff:
                continue
            path.unlink()
            orphaned += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Failed to delete orphaned file %s", path, exc_info=True)

    return {"expired": len(expired), "orphaned": orphaned}


async def resolve_target_access(
    user, *, channel_id: int | None = None, conversation_id: int | None = None
) -> tuple[int | None, int | None, int | None]:
    """Check that *user* may attach files to the given channel or conversation
    and return (server_id, channel_id, conversation_id).

    Exactly one target must be given. A non-participant of a conversation gets
    a 404, like the conversation endpoints, so ids can't be probed.
    """
    if (channel_id is None) == (conversation_id is None):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of channel_id or conversation_id")

    if channel_id is not None:
        channel = await Channel.get_or_none(id=channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        server = await Server.get_or_none(id=channel.server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Server not found")
        await require_permission(user, server, Permission.VIEW_CHANNEL)
        return server.id, channel.id, None

    conversation = await Conversation.get_or_none(id=conversation_id)
    if not conversation or not conversation.has_participant(user.id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return None, None, conversation.id
