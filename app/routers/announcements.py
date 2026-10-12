from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator
from pydantic_core import to_jsonable_python
from tortoise.expressions import Q

from ..middleware import get_current_user
from ..models.Announcement import Announcement, AnnouncementLevel
from ..models.AnnouncementDismissal import AnnouncementDismissal
from ..models.User import User
from ..platform import require_platform_admin

router = APIRouter(prefix="/api", tags=["announcements"])
admin_router = APIRouter(
    prefix="/admin/announcements",
    dependencies=[Depends(require_platform_admin)],
)

ANNOUNCEMENT_TEXT_MAX = 500
USER_LIST_DAYS = 30


# ── Pydantic schemas ────────────────────────────────────────────────

class AnnouncementCreate(BaseModel):
    text: str
    level: AnnouncementLevel
    expires_at: Optional[datetime] = None

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Text can't be empty")
        if len(value) > ANNOUNCEMENT_TEXT_MAX:
            raise ValueError(f"Text must be at most {ANNOUNCEMENT_TEXT_MAX} characters")
        return value

    @field_validator("expires_at")
    @classmethod
    def _expires_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Expiry must include a timezone")
        if value <= datetime.now(timezone.utc):
            raise ValueError("Expiry must be in the future")
        return value


class AnnouncementResponse(BaseModel):
    id: int
    text: str
    level: str
    created_at: datetime
    expires_at: Optional[datetime]
    ended: bool
    dismissed: bool


class AdminAuthor(BaseModel):
    id: int
    username: str


class AdminAnnouncementResponse(BaseModel):
    id: int
    text: str
    level: str
    created_at: datetime
    expires_at: Optional[datetime]
    ended: bool
    created_by: Optional[AdminAuthor]


# ── Helpers ─────────────────────────────────────────────────────────

def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _has_ended(announcement: Announcement, now: datetime) -> bool:
    expires_at = _aware(announcement.expires_at)
    return expires_at is not None and expires_at <= now


def _user_view(
    announcement: Announcement, now: datetime, *, dismissed: bool
) -> AnnouncementResponse:
    return AnnouncementResponse(
        id=announcement.id,
        text=announcement.text,
        level=announcement.level,
        created_at=announcement.created_at,
        expires_at=announcement.expires_at,
        ended=_has_ended(announcement, now),
        dismissed=dismissed,
    )


def _admin_view(announcement: Announcement, now: datetime) -> AdminAnnouncementResponse:
    author = announcement.created_by
    return AdminAnnouncementResponse(
        id=announcement.id,
        text=announcement.text,
        level=announcement.level,
        created_at=announcement.created_at,
        expires_at=announcement.expires_at,
        ended=_has_ended(announcement, now),
        created_by=AdminAuthor(id=author.id, username=author.username) if author else None,
    )


async def _get_or_404(announcement_id: int) -> Announcement:
    announcement = await Announcement.get_or_none(id=announcement_id).prefetch_related("created_by")
    if announcement is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return announcement


async def _send_to_everyone_online(request: Request, frame: dict) -> None:
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_users(comms.connection_manager.online_user_ids(), frame)


# ── User endpoints ──────────────────────────────────────────────────

@router.get("/announcements", response_model=list[AnnouncementResponse])
async def list_announcements(current_user: User = Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    rows = await Announcement.filter(
        created_at__gte=now - timedelta(days=USER_LIST_DAYS)
    ).order_by("-created_at", "-id")
    dismissed = set(await AnnouncementDismissal.filter(
        user_id=current_user.id, announcement_id__in=[row.id for row in rows]
    ).values_list("announcement_id", flat=True))
    return [_user_view(row, now, dismissed=row.id in dismissed) for row in rows]


@router.post("/announcements/{announcement_id}/dismiss", status_code=204)
async def dismiss_announcement(
    announcement_id: int, current_user: User = Depends(get_current_user)
):
    if not await Announcement.filter(id=announcement_id).exists():
        raise HTTPException(status_code=404, detail="Announcement not found")
    await AnnouncementDismissal.get_or_create(
        announcement_id=announcement_id, user_id=current_user.id)
    return Response(status_code=204)


# ── Admin endpoints ─────────────────────────────────────────────────

@admin_router.get("", response_model=list[AdminAnnouncementResponse])
async def list_all_announcements():
    now = datetime.now(timezone.utc)
    rows = await Announcement.all().order_by("-created_at", "-id").prefetch_related("created_by")
    return [_admin_view(row, now) for row in rows]


@admin_router.post("", response_model=AdminAnnouncementResponse, status_code=201)
async def create_announcement(
    body: AnnouncementCreate,
    request: Request,
    current_user: User = Depends(require_platform_admin),
):
    announcement = await Announcement.create(
        text=body.text,
        level=body.level.value,
        expires_at=body.expires_at,
        created_by=current_user,
    )
    now = datetime.now(timezone.utc)
    await _send_to_everyone_online(request, {
        "type": "announcement",
        "announcement": _user_view(announcement, now, dismissed=False).model_dump(mode="json"),
    })
    return _admin_view(announcement, now)


@admin_router.post("/{announcement_id}/end", response_model=AdminAnnouncementResponse)
async def end_announcement(announcement_id: int, request: Request):
    await _get_or_404(announcement_id)
    now = datetime.now(timezone.utc)
    changed = await Announcement.filter(id=announcement_id).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).update(expires_at=now)
    announcement = await _get_or_404(announcement_id)
    if changed:
        await _send_to_everyone_online(request, {
            "type": "announcement_ended",
            "id": announcement.id,
            "expires_at": to_jsonable_python(_aware(announcement.expires_at)),
        })
    return _admin_view(announcement, now)


router.include_router(admin_router)
