import logging
import re
import traceback
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse

from .services import recent_errors
from .services.error_counter import server_5xx

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

logger = logging.getLogger("app.errors")


async def request_context_middleware(request: Request, call_next):
    """Tag the request with an id and turn unhandled exceptions into a logged,
    JSON 500 that still passes through the CORS middleware."""
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    request_id_var.set(request_id)

    try:
        response = await call_next(request)
    except Exception as exc:
        user = getattr(request.state, "user", None)
        recent_errors.server.record(
            source="server",
            kind=type(exc).__name__,
            message=str(exc) or type(exc).__name__,
            method=request.method,
            path=request.url.path,
            user_id=user.id if user else None,
            request_id=request_id,
            stack=traceback.format_exc(),
        )
        logger.error(
            "Unhandled error %s %s request_id=%s user_id=%s",
            request.method,
            request.url.path,
            request_id,
            user.id if user else "-",
            exc_info=True,
        )
        response = JSONResponse(
            {"detail": "Internal server error", "request_id": request_id},
            status_code=500,
        )
    else:
        if response.status_code >= 500:
            user = getattr(request.state, "user", None)
            recent_errors.server.record(
                source="server",
                kind=f"http_{response.status_code}",
                message=f"HTTP {response.status_code}",
                method=request.method,
                path=request.url.path,
                user_id=user.id if user else None,
                request_id=request_id,
            )

    if response.status_code >= 500:
        server_5xx.record()
    response.headers["X-Request-ID"] = request_id
    return response
