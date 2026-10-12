import re
from datetime import datetime
from typing import Annotated, Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from pydantic import AfterValidator, BaseModel
from pydantic_core import PydanticCustomError
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.User import User
from ..services.message_changes import tombstone_channel_messages_of
from ..services.system_user import IMPORTED_USERNAME
from ..services.storage import ImageValidationError, storage_service
from ..services.usernames import Username

router = APIRouter(prefix="/users", tags=["users"])

AVATAR_MAX_PX = 256
BANNER_MAX_SIZE = (1200, 480)

_BANNER_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _check_banner_color(value: str) -> str:
    if not _BANNER_COLOR.fullmatch(value):
        raise PydanticCustomError("banner_color", "Use a colour like #1a2b3c.")
    return value.lower()


BannerColor = Annotated[str, AfterValidator(_check_banner_color)]


class UserUpdate(BaseModel):
    """Other fields are ignored: older clients send the whole user back. The password has
    no endpoint yet, and the profile's avatar and banner image are only written by their
    uploads. ``banner_color`` is the one profile key set here; null clears the banner."""
    username: Optional[Username] = None
    public_key: Optional[str] = None
    banner_color: Optional[BannerColor] = None


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


async def _update_profile(user_id: int, change: Callable[[dict], None]) -> User:
    """Apply *change* to the stored profile under a row lock, so concurrent writers to
    different keys (avatar, banner) can't overwrite each other."""
    async with in_transaction():
        user = await User.select_for_update(no_key=True).get_or_none(id=user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        profile = dict(user.profile or {})
        change(profile)
        user.profile = profile
        await user.save(update_fields=["profile"])
    return user


def _banner_color_setter(color: Optional[str]) -> Callable[[dict], None]:
    def change(profile: dict) -> None:
        profile.pop("banner_image", None)
        if color is None:
            profile.pop("banner_color", None)
        else:
            profile["banner_color"] = color

    return change


def _avatar_setter(url: str) -> Callable[[dict], None]:
    def change(profile: dict) -> None:
        profile["avatar"] = url

    return change


def _banner_image_setter(url: str) -> Callable[[dict], None]:
    def change(profile: dict) -> None:
        profile["banner_image"] = url
        profile.pop("banner_color", None)

    return change


def _clear_banner(profile: dict) -> None:
    profile.pop("banner_image", None)
    profile.pop("banner_color", None)


async def _notify_profile_changed(request: Request, user_id: int) -> None:
    if hasattr(request.app.state, "comms"):
        await request.app.state.comms.notify_user_invalidate(user_id)


async def _store_image(content: bytes, destination: str, max_size_px: int | tuple[int, int]) -> str:
    try:
        url = await storage_service.upload_image(content, destination, max_size_px=max_size_px)
    except ImageValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    if not url:
        raise HTTPException(status_code=500, detail="Failed to upload file")
    return url


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
    banner_sent = "banner_color" in update_data
    banner_color = update_data.pop("banner_color", None)
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

    if banner_sent:
        user = await _update_profile(user_id, _banner_color_setter(banner_color))

    await _notify_profile_changed(request, user_id)

    return UserResponse.from_user(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own account")

    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    async with in_transaction() as conn:
        await tombstone_channel_messages_of(conn, user.id)
        await user.delete(using_db=conn)


@router.post("/{user_id}/avatar", response_model=UserResponse)
async def upload_user_avatar(user_id: int, request: Request, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own avatar")

    content = await file.read()
    url = await _store_image(content, f"users/{user_id}/avatar", AVATAR_MAX_PX)
    user = await _update_profile(user_id, _avatar_setter(url))

    await _notify_profile_changed(request, user_id)

    return UserResponse.from_user(user)


@router.post("/{user_id}/banner", response_model=UserResponse)
async def upload_user_banner(user_id: int, request: Request, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own banner")

    content = await file.read()
    url = await _store_image(content, f"users/{user_id}/banner", BANNER_MAX_SIZE)
    user = await _update_profile(user_id, _banner_image_setter(url))

    await _notify_profile_changed(request, user_id)

    return UserResponse.from_user(user)


@router.delete("/{user_id}/banner", response_model=UserResponse)
async def delete_user_banner(user_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own banner")

    user = await _update_profile(user_id, _clear_banner)

    await _notify_profile_changed(request, user_id)

    return UserResponse.from_user(user)
