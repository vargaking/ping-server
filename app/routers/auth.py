import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from typing import Optional

from ..middleware import get_current_user
from ..models.Token import Token
from ..models.User import User
from .users import UserResponse

router = APIRouter(prefix="/auth", tags=["authentication"])


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
    await Token.create(user_id=user.id, token=access_token)

    is_dev = os.getenv("DEBUG", "").lower() == "true"
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=not is_dev,
        samesite="lax" if is_dev else "none",
        path="/"
    )

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
        token=access_token
    )

    is_dev = os.getenv("DEBUG", "").lower() == "true"
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=not is_dev,
        samesite="lax" if is_dev else "none",
        path="/"
    )

    return TokenResponse(
        access_token=access_token,
    )


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return UserResponse.from_user(current_user)
