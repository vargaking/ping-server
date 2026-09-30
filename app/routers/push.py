import base64
import binascii
import re

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, field_validator
from tortoise.exceptions import IntegrityError

from ..middleware import get_current_user
from ..models.PushSubscription import PushSubscription
from ..models.User import User
from ..services.push import is_allowed_endpoint, push

router = APIRouter(prefix="/api/push", tags=["push"])

MAX_ENDPOINT_LENGTH = 2048
MAX_SUBSCRIPTIONS_PER_USER = 10
P256DH_BYTES = 65
AUTH_BYTES = 16
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def _key_of_length(value: str, size: int) -> str:
    if not _BASE64URL.match(value):
        raise ValueError("must be base64url")
    unpadded = value.rstrip("=")
    try:
        decoded = base64.urlsafe_b64decode(unpadded + "=" * (-len(unpadded) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("must be base64url")
    if len(decoded) != size:
        raise ValueError(f"must decode to {size} bytes")
    return unpadded


class PushKeys(BaseModel):
    p256dh: str
    auth: str

    @field_validator("p256dh")
    @classmethod
    def _check_p256dh(cls, value: str) -> str:
        return _key_of_length(value, P256DH_BYTES)

    @field_validator("auth")
    @classmethod
    def _check_auth(cls, value: str) -> str:
        return _key_of_length(value, AUTH_BYTES)


class SubscriptionCreate(BaseModel):
    endpoint: str
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        if len(value) > MAX_ENDPOINT_LENGTH:
            raise ValueError("endpoint is too long")
        if not is_allowed_endpoint(value):
            raise ValueError("endpoint must be an https URL on a supported push service")
        return value


class SubscriptionDelete(BaseModel):
    endpoint: str


@router.get("/config")
async def get_push_config(current_user: User = Depends(get_current_user)):
    return {"enabled": push.enabled, "public_key": push.public_key}


@router.post("/subscriptions")
async def create_subscription(
    body: SubscriptionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """Register this browser for push. Idempotent by endpoint; an endpoint
    that belonged to another account (shared browser) moves to the caller."""
    fields = {"p256dh": body.keys.p256dh, "auth": body.keys.auth}
    existing = await PushSubscription.get_or_none(endpoint=body.endpoint)
    if existing is None:
        try:
            created = await PushSubscription.create(
                user_id=current_user.id, endpoint=body.endpoint, **fields)
        except IntegrityError:
            # Lost a create race for the same endpoint.
            existing = await PushSubscription.get(endpoint=body.endpoint)
        else:
            await _enforce_cap(current_user.id, keep_id=created.id)
            response.status_code = status.HTTP_201_CREATED
            return {"id": created.id}

    was_theirs = existing.user_id == current_user.id
    update = dict(fields)
    if not was_theirs:
        update.update(user_id=current_user.id, last_used_at=None)
    await PushSubscription.filter(id=existing.id).update(**update)
    if not was_theirs:
        await _enforce_cap(current_user.id, keep_id=existing.id)
    response.status_code = status.HTTP_200_OK
    return {"id": existing.id}


@router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    body: SubscriptionDelete,
    current_user: User = Depends(get_current_user),
):
    await PushSubscription.filter(
        endpoint=body.endpoint, user_id=current_user.id).delete()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _enforce_cap(user_id: int, *, keep_id: int) -> None:
    """Drop the least recently used subscriptions beyond the per-user cap."""
    subs = await PushSubscription.filter(user_id=user_id).exclude(id=keep_id)
    excess = len(subs) + 1 - MAX_SUBSCRIPTIONS_PER_USER
    if excess <= 0:
        return
    subs.sort(key=lambda s: s.last_used_at or s.created_at)
    await PushSubscription.filter(id__in=[s.id for s in subs[:excess]]).delete()
