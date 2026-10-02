from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, field_validator
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.ServerRequest import ServerRequest
from ..models.User import User
from ..platform import require_platform_admin
from ..services.push import push, server_request_payload
from ..services.server_name import clean_server_name
from ..settings import server_creation_mode
from .servers import create_server_for, server_response

router = APIRouter(prefix="/server-requests", tags=["server-requests"])

DESCRIPTION_MAX = 500
DECLINE_REASON_MAX = 500


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class ExpectedSize(StrEnum):
    UNDER_10 = "lt10"
    TEN_TO_FIFTY = "10to50"
    OVER_50 = "50plus"


class ServerRequestCreate(BaseModel):
    name: str
    description: str
    expected_size: ExpectedSize

    _name = field_validator("name")(clean_server_name)

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Description can't be empty")
        if len(value) > DESCRIPTION_MAX:
            raise ValueError(f"Description must be at most {DESCRIPTION_MAX} characters")
        return value


class ServerRequestDecline(BaseModel):
    reason: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if len(value) > DECLINE_REASON_MAX:
            raise ValueError(f"Reason must be at most {DECLINE_REASON_MAX} characters")
        return value or None


class ServerRequestOut(BaseModel):
    id: int
    name: str
    description: str
    expected_size: ExpectedSize
    status: RequestStatus
    decline_reason: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None
    server_id: Optional[int] = None

    @classmethod
    def from_request(cls, req: ServerRequest):
        return cls(
            id=req.id,
            name=req.name,
            description=req.description,
            expected_size=req.expected_size,
            status=req.status,
            decline_reason=req.decline_reason,
            created_at=req.created_at,
            decided_at=req.decided_at,
            server_id=req.server_id,
        )


class Requester(BaseModel):
    id: int
    username: str


class AdminServerRequestOut(ServerRequestOut):
    requester: Requester

    @classmethod
    def from_request(cls, req: ServerRequest):
        return cls(
            **ServerRequestOut.from_request(req).model_dump(),
            requester=Requester(id=req.user.id, username=req.user.username),
        )


class MyServerRequestState(BaseModel):
    mode: Literal["open", "waitlist"]
    can_create: bool
    request: Optional[ServerRequestOut] = None


_ALREADY_PENDING = "You already have a pending request"


@router.get("/me", response_model=MyServerRequestState)
async def get_my_request_state(current_user: User = Depends(get_current_user)):
    mode = server_creation_mode()
    latest = await ServerRequest.filter(user=current_user).exclude(
        status=RequestStatus.WITHDRAWN).first()
    return MyServerRequestState(
        mode=mode,
        can_create=mode == "open" or current_user.is_platform_admin,
        request=ServerRequestOut.from_request(latest) if latest else None,
    )


@router.post("", response_model=ServerRequestOut, status_code=status.HTTP_201_CREATED)
async def create_request(body: ServerRequestCreate, current_user: User = Depends(get_current_user)):
    if server_creation_mode() == "open":
        raise HTTPException(
            status_code=409, detail="Server creation is open; create the server directly")
    if await ServerRequest.filter(user=current_user, status=RequestStatus.PENDING).exists():
        raise HTTPException(status_code=409, detail=_ALREADY_PENDING)
    try:
        req = await ServerRequest.create(user=current_user, **body.model_dump())
    except IntegrityError:
        raise HTTPException(status_code=409, detail=_ALREADY_PENDING)
    return ServerRequestOut.from_request(req)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_request(current_user: User = Depends(get_current_user)):
    withdrawn = await ServerRequest.filter(
        user=current_user, status=RequestStatus.PENDING
    ).update(status=RequestStatus.WITHDRAWN)
    if not withdrawn:
        raise HTTPException(status_code=404, detail="No pending request")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("", response_model=list[AdminServerRequestOut])
async def list_requests(
    status_filter: Literal["pending", "all"] = Query("pending", alias="status"),
    _admin: User = Depends(require_platform_admin),
):
    requests = ServerRequest.all().prefetch_related("user")
    if status_filter == "pending":
        requests = requests.filter(status=RequestStatus.PENDING).order_by("created_at")
    return [AdminServerRequestOut.from_request(req) for req in await requests]


async def _load_request(request_id: int) -> ServerRequest:
    req = await ServerRequest.get_or_none(id=request_id).prefetch_related("user")
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


async def _decide(req: ServerRequest, admin: User, new_status: RequestStatus, **fields) -> None:
    """Move *req* out of pending. The filtered update makes two admins deciding
    at once resolve to one winner; the other gets a 409."""
    decided = await ServerRequest.filter(id=req.id, status=RequestStatus.PENDING).update(
        status=new_status, decided_at=datetime.now(timezone.utc), decided_by=admin, **fields)
    if decided != 1:
        raise HTTPException(status_code=409, detail="Request was already decided")
    await req.refresh_from_db()


@router.post("/{request_id}/approve", response_model=AdminServerRequestOut)
async def approve_request(
    request_id: int,
    request: Request,
    admin: User = Depends(require_platform_admin),
):
    req = await _load_request(request_id)
    async with in_transaction():
        await _decide(req, admin, RequestStatus.APPROVED)
        server = await create_server_for(req.user, name=req.name)
        req.server = server
        await req.save(update_fields=["server_id"])

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await server.fetch_related("server_users__user")
        added = await server_response(server, req.user)
        await comms.send_to_user(req.user.id, {
            "type": "server_request_updated",
            "request": ServerRequestOut.from_request(req).model_dump(mode="json"),
        })
        await comms.send_to_user(req.user.id, {
            "type": "server_added",
            "server": added.model_dump(mode="json"),
        })
        if push.enabled and not comms.connection_manager.is_active(req.user.id):
            tag = f"server-request-{req.id}"
            push.schedule(push.send_to_user(
                req.user.id,
                server_request_payload(
                    request_id=req.id, server_id=server.id, server_name=server.name),
                topic=tag, urgency="normal"))
    return AdminServerRequestOut.from_request(req)


@router.post("/{request_id}/decline", response_model=AdminServerRequestOut)
async def decline_request(
    request_id: int,
    request: Request,
    body: ServerRequestDecline = ServerRequestDecline(),
    admin: User = Depends(require_platform_admin),
):
    req = await _load_request(request_id)
    await _decide(req, admin, RequestStatus.DECLINED, decline_reason=body.reason)

    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.send_to_user(req.user.id, {
            "type": "server_request_updated",
            "request": ServerRequestOut.from_request(req).model_dump(mode="json"),
        })
    return AdminServerRequestOut.from_request(req)
