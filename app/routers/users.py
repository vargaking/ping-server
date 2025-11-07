from fastapi import APIRouter, HTTPException, status
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from ..models.User import User

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    password: str
    public_key: Optional[str] = None
    profile: dict = {}


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    public_key: Optional[str] = None
    profile: Optional[dict] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime
    is_active: bool
    public_key: Optional[str] = None
    profile: dict

    @classmethod
    def from_user(cls, user: User):
        return cls(
            id=user.id,
            username=user.username,
            created_at=user.created_at,
            is_active=user.is_active,
            public_key=user.public_key,
            profile=user.profile
        )


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate):
    user_data = user.model_dump()
    password = user_data.pop('password')
    user_obj = await User.create_with_password(password=password, **user_data)
    return UserResponse.from_user(user_obj)


@router.get("/", response_model=List[UserResponse])
async def get_users():
    users = await User.all()
    return [UserResponse.from_user(user) for user in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: int):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.from_user(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(user_id: int, user_update: UserUpdate):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user_update.model_dump(exclude_unset=True)
    if 'password' in update_data:
        user.set_password(update_data.pop('password'))

    await user.update_from_dict(update_data)
    await user.save()
    return UserResponse.from_user(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await user.delete()
