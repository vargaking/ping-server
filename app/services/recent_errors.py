from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

MAX_ENTRIES = 200
MAX_STACK_CHARS = 8000
MAX_MESSAGE_CHARS = 1000


@dataclass(frozen=True)
class ErrorEntry:
    at: datetime
    source: Literal["client", "server"]
    kind: str
    message: str
    method: str | None
    path: str | None
    user_id: int | None
    request_id: str | None
    stack: str | None


class RecentErrors:
    """Last MAX_ENTRIES errors of one source. Per-process, so it starts empty
    after a restart."""

    def __init__(self) -> None:
        self._entries: deque[ErrorEntry] = deque(maxlen=MAX_ENTRIES)

    def record(
        self,
        *,
        source: Literal["client", "server"],
        kind: str,
        message: str,
        method: str | None = None,
        path: str | None = None,
        user_id: int | None = None,
        request_id: str | None = None,
        stack: str | None = None,
    ) -> None:
        if stack is not None:
            # A Python traceback ends with the useful frames; a JS stack starts with them.
            stack = stack[-MAX_STACK_CHARS:] if source == "server" else stack[:MAX_STACK_CHARS]
        self._entries.append(ErrorEntry(
            at=datetime.now(timezone.utc),
            source=source,
            kind=kind,
            message=message[:MAX_MESSAGE_CHARS],
            method=method,
            path=path,
            user_id=user_id,
            request_id=request_id,
            stack=stack,
        ))

    def entries(self) -> list[ErrorEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()


client = RecentErrors()
server = RecentErrors()


def url_path(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).path or None


def grouped(entries: list[ErrorEntry]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for entry in sorted(entries, key=lambda e: e.at):
        key = (entry.source, entry.kind, entry.message, entry.method, entry.path)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "source": entry.source,
                "kind": entry.kind,
                "message": entry.message,
                "method": entry.method,
                "path": entry.path,
                "count": 0,
                "first_at": entry.at.isoformat(),
            }
        group["count"] += 1
        group["last_at"] = entry.at.isoformat()
        group["user_id"] = entry.user_id
        group["request_id"] = entry.request_id
        group["stack"] = entry.stack
    return sorted(groups.values(), key=lambda g: g["last_at"], reverse=True)
