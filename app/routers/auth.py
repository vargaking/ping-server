import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from typing import Optional

from ..middleware import get_current_user
from ..models.Token import Token
from ..models.User import User
from .users import UserResponse

router = APIRouter(prefix="/auth", tags=["authentication"])

# Session tokens live for 30 days; both the DB row and the browser cookie use
# this so they expire together.
TOKEN_TTL = timedelta(days=30)


def _set_session_cookie(response: Response, access_token: str) -> None:
    is_dev = os.getenv("DEBUG", "").lower() == "true"
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=int(TOKEN_TTL.total_seconds()),
        httponly=True,
        secure=not is_dev,
        samesite="lax" if is_dev else "none",
        path="/",
    )


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    public_key: Optional[str] = None
    profile: dict = {}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: RegisterRequest, response: Response):
    existing_user = await User.get_or_none(username=user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )

    user_dict = user_data.model_dump()
    password = user_dict.pop('password')
    user = await User.create_with_password(password=password, **user_dict)

    access_token = uuid.uuid4().hex
    await Token.create(
        user_id=user.id,
        token=access_token,
        expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
    )

    _set_session_cookie(response, access_token)

    return TokenResponse(access_token=access_token)


@router.post("/login", response_model=TokenResponse)
async def login(login_data: LoginRequest, response: Response):
    user = await User.get_or_none(username=login_data.username)
    if not user or not user.check_password(login_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )

    access_token = uuid.uuid4().hex

    await Token.create(
        user_id=user.id,
        token=access_token,
        expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
    )

    _set_session_cookie(response, access_token)

    return TokenResponse(
        access_token=access_token,
    )


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Invalidate the current session: delete the token row and clear the cookie.

    Intentionally does not require authentication so it stays idempotent — an
    already-expired or unknown cookie is simply cleared.
    """
    token = request.cookies.get("access_token")
    if token:
        await Token.filter(token=token).delete()
    response.delete_cookie("access_token", path="/")
    return {"detail": "Logged out"}


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return UserResponse.from_user(current_user)
