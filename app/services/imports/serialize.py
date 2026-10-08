"""The Import object the API and the WebSocket frames carry."""
from ...models.ServerImport import ServerImport

_PLAN_EXTRAS = ("authors", "seen")


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


def import_json(row: ServerImport, *, light: bool = False) -> dict:
    """*light* leaves out the plan, the result and the authors, which can be
    large; WebSocket frames use it."""
    source = None
    if row.source_platform is not None or row.source_name is not None:
        source = {"platform": row.source_platform, "server_name": row.source_name}
    return {
        "id": str(row.id),
        "status": row.status,
        "filename": row.filename,
        "size": row.size,
        "received": row.received,
        "source": source,
        "progress": row.progress,
        "failed_step": row.failed_step,
        "error": row.error,
        "plan": None if light else public_plan(row.plan),
        "result": None if light else row.result,
        "authors": [] if light else author_list(row),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
