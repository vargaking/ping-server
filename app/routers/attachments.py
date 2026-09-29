import logging
import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from ..middleware import get_current_user
from ..models.Attachment import Attachment
from ..models.Conversation import Conversation
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..services import attachments as attachment_service

logger = logging.getLogger("app.routers.attachments")

router = APIRouter(prefix="/attachments", tags=["attachments"])

_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")

# Uploads are served from the API origin, so the browser must never run or
# render them: no sniffing, a sandboxed empty CSP, and a forced download for
# everything that isn't a verified image.
_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, max-age=3600",
}


def _declared_content_type(value: str | None) -> str:
    media_type = (value or "").split(";")[0].strip().lower()
    if len(media_type) <= 255 and _MEDIA_TYPE.match(media_type):
        return media_type
    return "application/octet-stream"


def _format_limit(size: int) -> str:
    for unit, unit_size in (("MB", 1024 * 1024), ("KB", 1024)):
        if size >= unit_size:
            return f"{size / unit_size:.1f}".removesuffix(".0") + f" {unit}"
    return f"{size} bytes"


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    file: UploadFile = File(...),
    channel_id: int | None = Form(None),
    conversation_id: int | None = Form(None),
    current_user: User = Depends(get_current_user),
):
    """Upload a file for a channel or DM. It stays unattached until a message
    that references its id is sent."""
    server_id, channel_id, conversation_id = await attachment_service.resolve_target_access(
        current_user, channel_id=channel_id, conversation_id=conversation_id)

    limit = attachment_service.max_attachment_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File is too large (max {_format_limit(limit)})")
    if not data:
        raise HTTPException(status_code=422, detail="File is empty")

    sniffed = attachment_service.sniff_image(data)
    if sniffed:
        kind = "image"
        content_type, width, height = sniffed
    else:
        kind = "file"
        content_type = _declared_content_type(file.content_type)
        width = height = None

    subdir = (f"channels/{channel_id}" if channel_id is not None
              else f"conversations/{conversation_id}")
    storage_path = await attachment_service.store(data, subdir)
    attachment = await Attachment.create(
        uploader_id=current_user.id,
        server_id=server_id,
        channel_id=channel_id,
        conversation_id=conversation_id,
        filename=attachment_service.sanitize_filename(file.filename or ""),
        content_type=content_type,
        size=len(data),
        kind=kind,
        width=width,
        height=height,
        storage_path=storage_path,
    )
    return attachment.to_json()


async def _can_access(user: User, attachment: Attachment) -> bool:
    if attachment.channel_id is not None:
        return await UserToServer.filter(
            user_id=user.id, server_id=attachment.server_id).exists()
    if attachment.conversation_id is not None:
        conversation = await Conversation.get_or_none(id=attachment.conversation_id)
        return conversation is not None and conversation.has_participant(user.id)
    return False


@router.get("/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    current_user: User = Depends(get_current_user),
):
    """Serve an attachment to members of its channel or participants of its
    DM. Every failure is a 404 so ids can't be probed."""
    try:
        attachment_uuid = UUID(attachment_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Attachment not found")

    attachment = await Attachment.get_or_none(id=attachment_uuid)
    if attachment is None or not await _can_access(current_user, attachment):
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = (attachment_service.attachments_root() / attachment.storage_path).resolve()
    if not path.is_relative_to(attachment_service.attachments_root()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")

    if attachment.kind == "image":
        disposition = "inline"
    else:
        disposition = f"attachment; filename*=UTF-8''{quote(attachment.filename, safe='')}"

    return FileResponse(
        path,
        media_type=attachment.content_type,
        headers={**_SAFE_HEADERS, "Content-Disposition": disposition},
    )
