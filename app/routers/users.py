from fastapi import APIRouter, HTTPException, status, Request
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from ..models.User import User
from fastapi import UploadFile, File
from ..services.storage import storage_service

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


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime
    public_key: Optional[str] = None
    profile: dict

    @classmethod
    def from_user(cls, user: User):
        return cls(
            id=user.id,
            username=user.username,
            created_at=user.created_at,
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
async def update_user(user_id: int, user_update: UserUpdate, request: Request):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user_update.model_dump(exclude_unset=True)
    if 'password' in update_data:
        user.set_password(update_data.pop('password'))

    await user.update_from_dict(update_data)
    await user.save()

    # Broadcast update
    if hasattr(request.app.state, "comms"):
        user_response = UserResponse.from_user(user)
        await request.app.state.comms.broadcast_user_update(user_response.model_dump())

    return UserResponse.from_user(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await user.delete()


@router.post("/{user_id}/avatar", response_model=UserResponse)
async def upload_user_avatar(user_id: int, request: Request, file: UploadFile = File(...)):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    content = await file.read()
    url = await storage_service.upload_file(
        content,
        file.content_type,
        f"users/{user_id}/avatar"
    )

    if not url:
        raise HTTPException(status_code=500, detail="Failed to upload file")

    # Create a copy of the profile to ensure Tortoise ORM detects the change
    profile = user.profile.copy() if user.profile else {}
    profile['avatar'] = url
    user.profile = profile
    await user.save()

    # Broadcast update
    if hasattr(request.app.state, "comms"):
        user_response = UserResponse.from_user(user)
        await request.app.state.comms.broadcast_user_update(user_response.model_dump())

    return UserResponse.from_user(user)
