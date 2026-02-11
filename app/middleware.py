from fastapi import Request, HTTPException, status
from .models.User import User
from .models.Token import Token
from typing import Optional
import logging

logger = logging.getLogger("app.middleware")


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

    user = None
    token = request.cookies.get("access_token")

    if token:
        try:
            token_obj = await Token.get(token=token).prefetch_related('user')
            user = token_obj.user
        except Exception as e:
            logger.warning(f"Invalid token: {e}")

    request.state.user = user

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
