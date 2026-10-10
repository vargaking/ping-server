from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from pydantic import BaseModel
from tortoise.exceptions import IntegrityError

from ..middleware import get_current_user
from ..models.User import User
from ..services.system_user import IMPORTED_USERNAME
from ..services.storage import ImageValidationError, storage_service
from ..services.usernames import Username

router = APIRouter(prefix="/users", tags=["users"])


class UserUpdate(BaseModel):
    """Other fields are ignored: older clients send the whole user back. The password has
    no endpoint yet, and the profile's avatar is only written by the avatar upload."""
    username: Optional[Username] = None
    public_key: Optional[str] = None


USERNAME_TAKEN = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail=[{"loc": ["body", "username"], "msg": "That username is taken.", "type": "username_taken"}],
)


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


@router.get("/", response_model=List[UserResponse])
async def get_users(current_user: User = Depends(get_current_user)):
    users = await User.exclude(username=IMPORTED_USERNAME)
    return [UserResponse.from_user(user) for user in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, current_user: User = Depends(get_current_user)):
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.from_user(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(user_id: int, user_update: UserUpdate, request: Request, current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own profile")

    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user_update.model_dump(exclude_unset=True)
    if update_data.get("username") is None:
        update_data.pop("username", None)
    username = update_data.get("username")
    if username is not None and username != user.username:
        if await User.filter(username=username).exists():
            raise USERNAME_TAKEN

    user.update_from_dict(update_data)
    try:
        if update_data:
            await user.save(update_fields=list(update_data))
    except IntegrityError:
        raise USERNAME_TAKEN

    # Notify related users that this profile changed
    if hasattr(request.app.state, "comms"):
        await request.app.state.comms.notify_user_invalidate(user_id)

    return UserResponse.from_user(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own account")

    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await user.delete()


@router.post("/{user_id}/avatar", response_model=UserResponse)
async def upload_user_avatar(user_id: int, request: Request, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own avatar")

    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    content = await file.read()
    try:
        url = await storage_service.upload_image(
            content,
            f"users/{user_id}/avatar",
            max_size_px=256,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if not url:
        raise HTTPException(status_code=500, detail="Failed to upload file")

    # Create a copy of the profile to ensure Tortoise ORM detects the change
    profile = user.profile.copy() if user.profile else {}
    profile['avatar'] = url
    user.profile = profile
    await user.save(update_fields=["profile"])

    # Notify related users that this profile changed
    if hasattr(request.app.state, "comms"):
        await request.app.state.comms.notify_user_invalidate(user_id)

    return UserResponse.from_user(user)
