import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from ..middleware import get_optional_user
from ..models.User import User
from ..rate_limit import limiter
from ..services import recent_errors
from ..services.error_counter import client_errors
from ..settings import client_error_global_rate_limit, client_error_rate_limit

router = APIRouter(prefix="/api/client-errors", tags=["client-errors"])

logger = logging.getLogger("app.client")

MAX_USER_AGENT_LENGTH = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(value: Optional[str]) -> str:
    """Escape control characters so a client can't forge extra log lines."""
    if value is None:
        return "-"
    return _CONTROL_CHARS.sub(
        lambda m: _NAMED_ESCAPES.get(m.group(), f"\\x{ord(m.group()):02x}"), value)


class ClientErrorReport(BaseModel):
    kind: Literal["error", "unhandledrejection", "svelte"]
    message: str = Field(max_length=1000)
    stack: Optional[str] = Field(default=None, max_length=8000)
    url: Optional[str] = Field(default=None, max_length=2000)
    line: Optional[int] = None
    column: Optional[int] = Field(default=None, ge=0)


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(client_error_rate_limit)
@limiter.limit(client_error_global_rate_limit, key_func=lambda request: "client-errors")
async def report_client_error(
    report: ClientErrorReport,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
) -> Response:
    client_errors.record()
    recent_errors.client.record(
        source="client",
        kind=report.kind,
        message=report.message,
        path=recent_errors.url_path(report.url),
        user_id=user.id if user else None,
        request_id=request.state.request_id,
        stack=report.stack,
    )
    user_agent = request.headers.get("user-agent", "")[:MAX_USER_AGENT_LENGTH]
    logger.warning(
        "client_error kind=%s user_id=%s url=%s line=%s col=%s ua=%s message=%s stack=%s",
        report.kind,
        user.id if user else "-",
        _escape(report.url),
        report.line if report.line is not None else "-",
        report.column if report.column is not None else "-",
        _escape(user_agent or None),
        _escape(report.message),
        _escape(report.stack),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
