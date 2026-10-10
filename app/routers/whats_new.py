import re
from datetime import date, datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import AfterValidator, BaseModel, StrictStr
from pydantic_core import PydanticCustomError
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.User import User

router = APIRouter(prefix="/whats-new", tags=["whats-new"])

_ENTRY_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(-[2-9])?")


def _check_entry_id(value: str) -> str:
    if _ENTRY_ID.fullmatch(value):
        try:
            date.fromisoformat(value[:10])
            return value
        except ValueError:
            pass
    raise PydanticCustomError("entry_id", "Use YYYY-MM-DD or YYYY-MM-DD-N (N 2–9).")


EntryId = Annotated[StrictStr, AfterValidator(_check_entry_id)]


class WhatsNewUpdate(BaseModel):
    last_seen_id: EntryId


class WhatsNewState(BaseModel):
    last_seen_id: Optional[str]
    created_on: date


def _state(user: User, last_seen_id: Optional[str]) -> WhatsNewState:
    created_at = user.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return WhatsNewState(
        last_seen_id=last_seen_id,
        created_on=created_at.astimezone(timezone.utc).date(),
    )


@router.get("/state", response_model=WhatsNewState)
async def get_state(user: User = Depends(get_current_user)):
    return _state(user, user.whats_new_seen_id)


@router.put("/state", response_model=WhatsNewState)
async def put_state(body: WhatsNewUpdate, user: User = Depends(get_current_user)):
    await User.filter(
        Q(id=user.id) & (Q(whats_new_seen_id__isnull=True) | Q(whats_new_seen_id__lt=body.last_seen_id))
    ).update(whats_new_seen_id=body.last_seen_id)
    stored = await User.get(id=user.id).values_list("whats_new_seen_id", flat=True)
    return _state(user, stored)
