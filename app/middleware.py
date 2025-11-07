from fastapi import Request, HTTPException, status, Depends
from fastapi.responses import JSONResponse
import jwt as jose
import os
from .models.User import User
from typing import Optional
import logging

logger = logging.getLogger("app.middleware")

SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY", "your-secret-key-change-this-in-production")
ALGORITHM = "HS256"


async def auth_middleware(request: Request, call_next):
    """
    Middleware to automatically identify users from tokens and add user to request state.
    This middleware will NOT block requests for unauthenticated users, it just adds
    user information when available.
    """
    # Skip auth for public endpoints
    public_paths = ["/", "/docs", "/redoc",
                    "/openapi.json", "/auth/login", "/auth/register"]

    if request.url.path in public_paths:
        response = await call_next(request)
        return response

    # Try to get user from token
    user = None
    token = None

    # Try to get token from Authorization header first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]

    # If no header token, try to get from cookie
    if not token:
        token = request.cookies.get("access_token")

    if token:
        try:
            payload = jose.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username: str = payload.get("sub")
            if username:
                user = await User.get_or_none(username=username)
        except jose.PyJWTError as e:
            # Invalid token, but we don't block the request; log at debug level
            logger.debug("Invalid JWT token: %s", e)

    # Add user to request state
    request.state.user = user

    response = await call_next(request)
    return response


async def get_current_user_from_state(request: Request) -> Optional[User]:
    """
    Helper function to get the current user from request state.
    Returns None if no user is authenticated.
    """
    return getattr(request.state, 'user', None)


async def require_auth(request: Request) -> User:
    """
    Helper function that requires authentication.
    Raises HTTPException if no user is authenticated.
    """
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    return user


def get_current_user(request: Request) -> User:
    """
    FastAPI dependency to get the current authenticated user.
    Raises HTTPException if no user is authenticated.
    Use this as a dependency in your route handlers: user = Depends(get_current_user)
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
    FastAPI dependency to optionally get the current authenticated user.
    Returns None if no user is authenticated (doesn't raise an exception).
    Use this as a dependency: user = Depends(get_optional_user)
    """
    return getattr(request.state, 'user', None)
