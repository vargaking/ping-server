import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request, status

from .models.Token import Token
from .models.User import User

logger = logging.getLogger("app.middleware")


async def resolve_user_from_token(token: Optional[str]) -> Optional[User]:
    """Look up the user that owns the session *token*, or ``None``.

    Single source of truth for cookie -> user resolution, shared by the HTTP
    middleware and the WebSocket handshake. An expired token is treated as
    unauthenticated and its row is deleted, so this also covers /ws.
    """
    if not token:
        return None
    token_obj = await Token.get_or_none(token=token).prefetch_related("user")
    if token_obj is None:
        logger.warning("Rejected unknown session token")
        return None
    if token_obj.expires_at is not None and token_obj.expires_at < datetime.now(timezone.utc):
        logger.info("Rejected and deleted expired session token")
        await token_obj.delete()
        return None
    return token_obj.user


async def auth_middleware(request: Request, call_next):
    """
    HTTP middleware that resolves the current user from the access_token cookie
    and stores it on ``request.state.user``.  It never blocks a request on its
    own — use the ``get_current_user`` dependency to enforce authentication.
    """
    public_paths = {"/", "/docs", "/redoc",
                    "/openapi.json", "/auth/login", "/auth/register"}

    if request.url.path in public_paths:
        response = await call_next(request)
        return response

    request.state.user = await resolve_user_from_token(
        request.cookies.get("access_token"))

    response = await call_next(request)
    return response


def get_current_user(request: Request) -> User:
    """
    FastAPI dependency — returns the authenticated user or raises 401.
    Usage:  ``current_user: User = Depends(get_current_user)``
    """
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    return user


def get_optional_user(request: Request) -> Optional[User]:
    """
    FastAPI dependency — returns the authenticated user or ``None``.
    Usage:  ``user: Optional[User] = Depends(get_optional_user)``
    """
    return getattr(request.state, 'user', None)
