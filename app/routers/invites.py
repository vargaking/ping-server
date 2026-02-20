from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Invite import Invite
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..utils import require_membership

router = APIRouter(prefix="/invites", tags=["invites"])


# ── Pydantic schemas ────────────────────────────────────────────────

class InviteCreate(BaseModel):
    server_id: int
    valid_until: Optional[datetime] = None
    max_uses: Optional[int] = None
    password: Optional[str] = None


class InviteUpdate(BaseModel):
    valid_until: Optional[datetime] = None
    max_uses: Optional[int] = None
    is_active: Optional[bool] = None


class InviteResponse(BaseModel):
    id: UUID
    server_id: int
    created_by_id: int
    created_at: datetime
    valid_until: Optional[datetime]
    max_uses: Optional[int]
    use_count: int
    is_active: bool
    has_password: bool

    @classmethod
    def from_invite(cls, invite: Invite):
        return cls(
            id=invite.id,
            server_id=invite.server_id,
            created_by_id=invite.created_by_id,
            created_at=invite.created_at,
            valid_until=invite.valid_until,
            max_uses=invite.max_uses,
            use_count=invite.use_count,
            is_active=invite.is_active,
            has_password=invite.password_hash is not None,
        )


class InvitePublicResponse(BaseModel):
    """Limited info returned to non-members checking an invite."""
    id: UUID
    server_id: int
    is_valid: bool
    has_password: bool


class InviteUseRequest(BaseModel):
    password: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────

def _is_valid(invite: Invite) -> bool:
    if not invite.is_active:
        return False
    if invite.valid_until and invite.valid_until < datetime.now(timezone.utc):
        return False
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        return False
    return True


# ── Endpoints ───────────────────────────────────────────────────────

@router.post("/", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: InviteCreate,
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=body.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    await require_membership(current_user, server)

    invite = Invite(
        server=server,
        created_by=current_user,
        valid_until=body.valid_until,
        max_uses=body.max_uses,
    )
    if body.password:
        invite.set_password(body.password)

    await invite.save()
    return InviteResponse.from_invite(invite)


@router.get("/server/{server_id}", response_model=List[InviteResponse])
async def list_server_invites(
    server_id: int,
    current_user: User = Depends(get_current_user),
):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    await require_membership(current_user, server)

    invites = await Invite.filter(server=server)
    return [InviteResponse.from_invite(inv) for inv in invites]


@router.get("/{invite_id}", response_model=InvitePublicResponse)
async def get_invite(
    invite_id: UUID,
    current_user: User = Depends(get_current_user),
):
    invite = await Invite.get_or_none(id=invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    return InvitePublicResponse(
        id=invite.id,
        server_id=invite.server_id,
        is_valid=_is_valid(invite),
        has_password=invite.password_hash is not None,
    )


@router.put("/{invite_id}", response_model=InviteResponse)
async def update_invite(
    invite_id: UUID,
    body: InviteUpdate,
    current_user: User = Depends(get_current_user),
):
    invite = await Invite.get_or_none(id=invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    server = await Server.get_or_none(id=invite.server_id)
    await require_membership(current_user, server)

    update_data = body.model_dump(exclude_unset=True)
    await invite.update_from_dict(update_data)
    await invite.save()
    return InviteResponse.from_invite(invite)


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invite(
    invite_id: UUID,
    current_user: User = Depends(get_current_user),
):
    invite = await Invite.get_or_none(id=invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    server = await Server.get_or_none(id=invite.server_id)
    await require_membership(current_user, server)

    await invite.delete()


@router.post("/{invite_id}/use", status_code=status.HTTP_200_OK)
async def use_invite(
    invite_id: UUID,
    body: InviteUseRequest = InviteUseRequest(),
    current_user: User = Depends(get_current_user),
):
    invite = await Invite.get_or_none(id=invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    # Validity checks
    if not _is_valid(invite):
        raise HTTPException(status_code=410, detail="Invite is no longer valid")

    # Password check
    if invite.password_hash:
        if not body.password:
            raise HTTPException(status_code=401, detail="Password required")
        if not invite.check_password(body.password):
            raise HTTPException(status_code=401, detail="Incorrect password")

    # Already a member?
    server = await Server.get(id=invite.server_id)
    existing = await UserToServer.filter(user=current_user, server=server).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already a member of this server")

    # Join the server
    await UserToServer.create(user=current_user, server=server)
    invite.use_count += 1
    await invite.save()

    return {"detail": "Successfully joined the server", "server_id": server.id}
