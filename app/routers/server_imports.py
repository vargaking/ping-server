import errno
import shutil
from datetime import datetime, timezone
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, field_validator
from tortoise.queryset import QuerySet

from ..middleware import get_current_user
from ..models.Server import Server
from ..models.ServerImport import ServerImport
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..permissions import server_from_path
from ..services.attachments import attachments_root, sanitize_filename
from ..services.imports import storage
from ..services.imports.runner import ImportRunner
from ..services.bundle.importer import Visibility
from ..services.imports.serialize import import_json, planned_messages
from ..services.system_user import IMPORTED_USERNAME
from ..settings import import_chunk_bytes, import_max_bytes
from ..utils import require_owner

router = APIRouter(prefix="/servers/{server_id}/import", tags=["server-imports"])

RUNNING = ("unpacking", "importing")
ATTACHMENT_SPACE_MARGIN = 256 * 1024 * 1024


async def owned_server(
    server_id: int,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(server_from_path),
) -> Server:
    require_owner(current_user, server)
    return server


def _require_enabled() -> None:
    if import_max_bytes() == 0:
        raise HTTPException(status_code=403, detail="Imports are turned off on this server")


def _runner(request: Request) -> ImportRunner:
    return request.app.state.import_runner


async def _load(server: Server, import_id: str) -> ServerImport:
    try:
        key = UUID(import_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Import not found")
    row = await ServerImport.get_or_none(id=key, server_id=server.id)
    if row is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return row


async def _delete_imports(imports: QuerySet[ServerImport]) -> None:
    ids = await imports.values_list("id", flat=True)
    if ids:
        await ServerImport.filter(id__in=ids).delete()
        await storage.remove_import_dirs(ids)


def _progress(phase: str, total: int = 0) -> dict:
    return {"phase": phase, "done": 0, "total": total, "label": None}


class ImportCreate(BaseModel):
    filename: str
    size: int

    @field_validator("filename")
    @classmethod
    def _zip_name(cls, value: str) -> str:
        name = sanitize_filename(value)
        if not name.lower().endswith(".zip"):
            raise ValueError("The file must be a .zip")
        return name

    @field_validator("size")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("The file is empty")
        return value


class AuthorsUpdate(BaseModel):
    authors: dict[str, int | None]


class ImportStart(BaseModel):
    # Absent or null keeps the stored selection; a dict, even an empty one, replaces it.
    private_channels: dict[str, Visibility] | None = None


@router.get("")
async def get_import(server: Server = Depends(owned_server)):
    latest = await ServerImport.filter(server_id=server.id).order_by("-created_at").first()
    return {
        "limits": {"max_bytes": import_max_bytes(), "chunk_bytes": import_chunk_bytes()},
        "import": import_json(latest) if latest else None,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_import(
    body: ImportCreate,
    server: Server = Depends(owned_server),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()
    if await ServerImport.filter(server_id=server.id, status__in=RUNNING).exists():
        raise HTTPException(status_code=409, detail="An import is already running")
    limit = import_max_bytes()
    if body.size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"The file is larger than the limit ({storage.human_size(limit)})")
    await _delete_imports(ServerImport.filter(server_id=server.id).exclude(status="done"))
    row = await ServerImport.create(
        server_id=server.id, created_by_id=current_user.id, status="uploading",
        filename=body.filename, size=body.size)
    return import_json(row)


async def _read_piece(request: Request, limit: int) -> bytes:
    too_large = HTTPException(
        status_code=413, detail=f"The piece is larger than the limit ({storage.human_size(limit)})")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    parts = []
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > limit:
            raise too_large
        parts.append(part)
    return b"".join(parts)


@router.put("/{import_id}/data")
async def upload_piece(
    import_id: str,
    request: Request,
    offset: int = Query(ge=0),
    server: Server = Depends(owned_server),
):
    _require_enabled()
    row = await _load(server, import_id)
    waiting = HTTPException(status_code=409, detail="This import is not waiting for data")
    if row.status != "uploading":
        raise waiting
    piece = await _read_piece(request, import_chunk_bytes())
    if not piece:
        raise HTTPException(status_code=422, detail="The piece is empty")
    if offset + len(piece) > row.size:
        raise HTTPException(status_code=413, detail="The piece goes past the end of the file")

    runner = _runner(request)
    async with runner.upload_lock(row.id):
        current = await ServerImport.get_or_none(id=row.id)
        if current is None or current.status != "uploading":
            raise waiting
        try:
            received = await anyio.to_thread.run_sync(
                storage.append_piece, row.id, offset, piece)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise HTTPException(status_code=507, detail="Not enough disk space on this server")
            raise
        if received is None:
            raise HTTPException(status_code=409, detail="Wrong offset")
        complete = received == row.size
        fields = {"received": received, "updated_at": datetime.now(timezone.utc)}
        if complete:
            fields.update(status="unpacking", progress=_progress("unpacking"))
        if not await ServerImport.filter(id=row.id, status="uploading").update(**fields):
            if not await ServerImport.filter(id=row.id).exists():
                await storage.remove_import_dirs([row.id])
            raise waiting
    if complete:
        runner.start_unpack(row.id)
    return {"received": received, "status": "unpacking" if complete else "uploading"}


async def _checked_mapping(
    server: Server, row: ServerImport, requested: dict[str, int | None],
) -> dict[str, int]:
    known = {a["id"] for a in (row.plan or {}).get("authors", [])}
    wanted = {user_id for user_id in requested.values() if user_id is not None}
    members = set(await UserToServer.filter(
        server_id=server.id, user_id__in=wanted).values_list("user_id", flat=True))
    system = await User.get_or_none(username=IMPORTED_USERNAME)
    mapping = {}
    for author_id, user_id in requested.items():
        if author_id not in known:
            raise HTTPException(
                status_code=422, detail=f"{author_id} is not an author of this import")
        if user_id is None:
            continue
        if system is not None and user_id == system.id:
            raise HTTPException(
                status_code=422, detail="The import account can't be mapped to an author")
        if user_id not in members:
            raise HTTPException(
                status_code=422, detail=f"User {user_id} is not a member of this server")
        mapping[author_id] = user_id
    return mapping


@router.put("/{import_id}/authors")
async def set_authors(
    import_id: str,
    body: AuthorsUpdate,
    request: Request,
    server: Server = Depends(owned_server),
):
    _require_enabled()
    row = await _load(server, import_id)
    locked = HTTPException(status_code=409, detail="The import can't be changed right now")
    if row.status not in ("ready", "done"):
        raise locked
    mapping = await _checked_mapping(server, row, body.authors)
    now = datetime.now(timezone.utc)
    if row.status == "ready":
        if not await ServerImport.filter(id=row.id, status="ready").update(
                authors=mapping, updated_at=now):
            raise locked
    elif mapping != (row.authors or {}) or row.error:
        total = planned_messages(row.plan)
        if not await ServerImport.filter(id=row.id, status="done").update(
                authors=mapping, status="importing", progress=_progress("authors", total),
                error=None, updated_at=now):
            raise locked
        _runner(request).start_handover(row.id)
    return import_json(await ServerImport.get(id=row.id))


def _checked_private(
    plan: dict, requested: dict[str, Visibility], retrying: bool = False,
) -> None:
    offered = {
        c["source_id"] for c in plan.get("channels", [])
        if c.get("private") and c.get("private_action") is not None}
    for source_id in requested:
        if source_id not in offered:
            raise HTTPException(
                status_code=422, detail=f"{source_id} is not a private channel of this import")
    if retrying:
        stored = plan.get("private_selection") or {}
        for source_id, visibility in requested.items():
            if source_id in stored and stored[source_id] != visibility:
                raise HTTPException(
                    status_code=422,
                    detail=f"{source_id}: the channel may already exist; "
                           "its visibility can't change on a retry")


@router.post("/{import_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_import(
    import_id: str,
    request: Request,
    body: ImportStart | None = None,
    server: Server = Depends(owned_server),
):
    _require_enabled()
    row = await _load(server, import_id)
    if not (row.status == "ready" or (row.status == "failed" and row.failed_step == "importing")):
        raise HTTPException(status_code=409, detail="The import isn't ready to start")
    plan = row.plan or {}
    requested = body.private_channels if body else None
    if requested is not None:
        _checked_private(plan, requested, retrying=row.status == "failed")
    selection = dict(plan.get("private_selection") or {}) if requested is None else dict(requested)
    plan = {**plan, "private_selection": selection}
    private_bytes = sum(
        c["attachment_bytes"] for c in plan.get("channels", []) if c["source_id"] in selection)
    needed = (plan.get("totals", {}).get("attachment_bytes", 0) + private_bytes
              + ATTACHMENT_SPACE_MARGIN)
    if needed > shutil.disk_usage(attachments_root()).free:
        raise HTTPException(status_code=409, detail="Not enough disk space for the attachments")
    if not await ServerImport.filter(id=row.id, status=row.status).update(
            plan=plan, status="importing", progress=_progress("importing", planned_messages(plan)),
            error=None, failed_step=None, updated_at=datetime.now(timezone.utc)):
        raise HTTPException(status_code=409, detail="The import isn't ready to start")
    await _delete_imports(
        ServerImport.filter(server_id=server.id).exclude(id=row.id).exclude(status__in=RUNNING))
    _runner(request).start_import(row.id)
    return import_json(await ServerImport.get(id=row.id))


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import(import_id: str, server: Server = Depends(owned_server)):
    row = await _load(server, import_id)
    if not await ServerImport.filter(id=row.id).exclude(status__in=RUNNING).delete():
        raise HTTPException(status_code=409, detail="The import is running")
    await storage.remove_import_dirs([row.id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
