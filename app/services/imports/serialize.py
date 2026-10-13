"""The Import object the API and the WebSocket frames carry."""
from datetime import datetime, timedelta, timezone

from ...models.ServerImport import ServerImport
from ...models.ServerImportAuthorChange import ServerImportAuthorChange

HANDOVER_FAILED = "Updating the authors failed. Try again or ask the server owner."
UPLOAD_ACTIVE_FOR = timedelta(seconds=30)
_PLAN_EXTRAS = ("authors", "seen", "private_seen", "private_selection")


def planned_messages(plan: dict | None) -> int:
    """The messages a run of this plan looks at: what the check saw, plus the
    selected private channels."""
    plan = plan or {}
    seen = plan.get("private_seen") or {}
    return plan.get("seen", 0) + sum(seen.get(i, 0) for i in plan.get("private_selection") or {})


def public_plan(plan: dict | None) -> dict | None:
    if plan is None:
        return None
    return {key: value for key, value in plan.items() if key not in _PLAN_EXTRAS}


def author_list(row: ServerImport) -> list[dict]:
    mapping = row.authors or {}
    return [
        {"id": a["id"], "name": a["name"], "messages": a["messages"],
         "user_id": mapping.get(a["id"])}
        for a in (row.plan or {}).get("authors", [])]


def upload_active(row: ServerImport, now: datetime | None = None) -> bool:
    """Whether an upload is being written right now: a piece arrived recently."""
    return (row.status == "uploading"
            and (now or datetime.now(timezone.utc)) - row.updated_at < UPLOAD_ACTIVE_FOR)


def _source(row: ServerImport) -> dict | None:
    if row.source_platform is None and row.source_name is None:
        return None
    return {"platform": row.source_platform, "server_name": row.source_name}


def import_json(row: ServerImport, *, light: bool = False) -> dict:
    """*light* leaves out the plan, the result and the authors, which can be
    large; WebSocket frames use it."""
    return {
        "id": str(row.id),
        "status": row.status,
        "filename": row.filename,
        "size": row.size,
        "received": row.received,
        "upload_active": upload_active(row),
        "source": _source(row),
        "private_channels": dict((row.plan or {}).get("private_selection") or {}),
        "progress": row.progress,
        "failed_step": row.failed_step,
        "error": row.error,
        "plan": None if light else public_plan(row.plan),
        "result": None if light else row.result,
        "authors": [] if light else author_list(row),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def holder_import_json(row: ServerImport) -> dict:
    """What a member who may match authors but isn't the owner sees of a hand-over.
    The progress label and the error can name source channels, so neither is passed on."""
    return {
        "id": str(row.id),
        "status": row.status,
        "progress": None if row.progress is None else {**row.progress, "label": None},
        "error": HANDOVER_FAILED if row.error else None,
        "updated_at": row.updated_at.isoformat(),
    }


def authors_import_json(row: ServerImport) -> dict:
    return holder_import_json(row) | {"source": _source(row), "authors": author_list(row)}


def change_json(change: ServerImportAuthorChange) -> dict:
    return {
        "id": change.id,
        "author_id": change.author_id,
        "author_name": change.author_name,
        "actor_id": change.actor_id,
        "from_user_id": change.from_user_id,
        "to_user_id": change.to_user_id,
        "created_at": change.created_at.isoformat(),
    }
