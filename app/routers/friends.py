from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from ..middleware import get_current_user
from ..models.User import User
from ..rate_limit import limiter, user_key
from ..services import friends
from ..services.friends import Change
from ..services.system_user import IMPORTED_USERNAME
from ..settings import friend_request_rate_limit
from .users import UserResponse

router = APIRouter(prefix="/friends", tags=["friends"])

_NO_REQUEST = "No friend request from this user"


class FriendOut(BaseModel):
    user: UserResponse
    since: datetime


class FriendRequestOut(BaseModel):
    user: UserResponse
    created_at: datetime


class FriendsList(BaseModel):
    friends: list[FriendOut]
    incoming: list[FriendRequestOut]
    outgoing: list[FriendRequestOut]


class FriendshipResult(BaseModel):
    user: UserResponse
    state: friends.FriendState
    created_at: datetime


class FriendRequestCreate(BaseModel):
    user_id: Optional[int] = None
    username: Optional[str] = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.user_id is None) == (self.username is None):
            raise ValueError("Send either user_id or username")
        return self


def _result(change: Change, other: User) -> FriendshipResult:
    return FriendshipResult(
        user=UserResponse.from_user(other),
        state=friends.state_for(change.row, change.actor_id),
        created_at=change.at,
    )


async def _announce(
    request: Request, change: Change, me: User, other: User | None = None,
) -> None:
    if change.reason is None:
        return
    other = other or await User.get_or_none(id=change.other_id)
    if other is not None:
        await friends.announce(
            getattr(request.app.state, "comms", None), change, me, other)


@router.get("/", response_model=FriendsList)
async def list_friends(current_user: User = Depends(get_current_user)):
    rows, users = await friends.list_for(current_user.id)
    out = FriendsList(friends=[], incoming=[], outgoing=[])
    for row in sorted(rows, key=lambda r: r.accepted_at or r.created_at, reverse=True):
        user = UserResponse.from_user(users[row.other_user_id(current_user.id)])
        state = friends.state_for(row, current_user.id)
        if state == friends.FriendState.FRIENDS:
            out.friends.append(FriendOut(user=user, since=row.accepted_at))
        else:
            target = out.outgoing if state == friends.FriendState.OUTGOING else out.incoming
            target.append(FriendRequestOut(user=user, created_at=row.created_at))
    return out


@router.post("/requests", response_model=FriendshipResult)
@limiter.limit(friend_request_rate_limit, key_func=user_key)
async def send_request(
    request: Request,
    response: Response,
    body: FriendRequestCreate,
    current_user: User = Depends(get_current_user),
):
    """Ask someone to be friends. If they already asked me, we become friends."""
    if body.user_id is not None:
        other = await User.get_or_none(id=body.user_id)
    else:
        other = await User.get_or_none(username=body.username)
    if other is None or other.username == IMPORTED_USERNAME:
        raise HTTPException(status_code=404, detail="User not found")
    if other.id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't add yourself")

    try:
        change = await friends.send_request(current_user.id, other.id)
    except friends.FriendshipConflict:
        raise HTTPException(status_code=409, detail="Try again")
    await _announce(request, change, current_user, other)
    return _result(change, other)


@router.post("/{user_id}/accept", response_model=FriendshipResult)
async def accept_request(
    user_id: int, request: Request, current_user: User = Depends(get_current_user),
):
    other = await User.get_or_none(id=user_id)
    change = await friends.accept(current_user.id, user_id) if other else None
    if change is None:
        raise HTTPException(status_code=404, detail=_NO_REQUEST)
    await _announce(request, change, current_user, other)
    return _result(change, other)


@router.post("/{user_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_request(
    user_id: int, request: Request, current_user: User = Depends(get_current_user),
):
    change = await friends.decline(current_user.id, user_id)
    await _announce(request, change, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{user_id}/request", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_request(
    user_id: int, request: Request, current_user: User = Depends(get_current_user),
):
    change = await friends.cancel(current_user.id, user_id)
    await _announce(request, change, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_friend(
    user_id: int, request: Request, current_user: User = Depends(get_current_user),
):
    change = await friends.remove(current_user.id, user_id)
    await _announce(request, change, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
