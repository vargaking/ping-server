from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Call import Call
from ..models.Conversation import Conversation
from ..models.User import User
from ..rate_limit import limiter, user_key
from ..services.call_rooms import room_name
from ..services.calls import (
    CALL_SOURCES,
    TERMINAL,
    CallAlreadyActive,
    CallEnded,
    CallNotFound,
    CallService,
    NotCallee,
    TooManyCalls,
    call_json,
)
from ..services.voice_presence import remove_from_voice
from ..settings import start_call_rate_limit
from . import voice

router = APIRouter(prefix="/api/calls", tags=["calls"])


class CallStart(BaseModel):
    conversation_id: int


class CallOut(BaseModel):
    id: int
    conversation_id: int
    message_id: str
    caller_id: int
    callee_id: int
    state: str
    reason: str | None
    created_at: str
    ring_expires_at: str
    answered_at: str | None
    ended_at: str | None
    duration_s: int | None

    @classmethod
    def of(cls, call: Call) -> "CallOut":
        return cls(**call_json(call))


class ActiveCalls(BaseModel):
    calls: list[CallOut]


def _service(request: Request) -> CallService:
    service = getattr(request.app.state, "calls", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Calls are not available")
    return service


def _require_livekit() -> None:
    if not voice.livekit_configured():
        raise HTTPException(status_code=503, detail="Voice service is not configured")


async def _get_call(service: CallService, user: User, call_id: int) -> Call:
    try:
        return await service.get_for(user.id, call_id)
    except CallNotFound:
        raise HTTPException(status_code=404, detail="Call not found")


@router.post("", response_model=CallOut)
@limiter.limit(start_call_rate_limit, key_func=user_key)
async def start_call(
    body: CallStart,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> CallOut:
    """Start a call in a DM, or return the one already open there. If that call is
    ringing for the caller of this request, it becomes active: two people calling
    each other end up in one call."""
    service = _service(request)
    _require_livekit()
    conversation = await Conversation.get_or_none(id=body.conversation_id)
    if conversation is None or not conversation.has_participant(current_user.id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        call = await service.start(current_user.id, conversation)
    except TooManyCalls as exc:
        raise HTTPException(
            status_code=429, detail="Too many unanswered calls",
            headers={"Retry-After": str(exc.retry_after)})
    return CallOut.of(call)


@router.get("/active", response_model=ActiveCalls)
async def active_calls(
    request: Request, current_user: User = Depends(get_current_user),
) -> ActiveCalls:
    calls = await _service(request).active_for(current_user.id)
    return ActiveCalls(calls=[CallOut.of(call) for call in calls])


@router.post("/{call_id}/token", response_model=voice.VoiceTokenResponse)
async def create_call_token(
    call_id: int, request: Request, current_user: User = Depends(get_current_user),
) -> voice.VoiceTokenResponse:
    """Mint a LiveKit token for a call's room. For the callee of a ringing call
    this is the answer. Leaves any voice channel and any other call."""
    service = _service(request)
    _require_livekit()
    call = await _get_call(service, current_user, call_id)
    try:
        await service.take_token(current_user.id, call)
    except CallEnded:
        raise HTTPException(status_code=409, detail="Call has ended")

    presence = getattr(request.app.state, "voice_presence", None)
    if presence is not None:
        await remove_from_voice(presence, presence.channels_of(current_user.id), current_user.id)
    await service.leave_rooms(current_user.id, except_call_id=call.id)

    room = room_name(call.id)
    token = voice.mint_token(current_user, room, CALL_SOURCES)
    return voice.VoiceTokenResponse(token=token, url=voice.LIVEKIT_URL, room=room)


@router.post("/{call_id}/decline", response_model=CallOut)
async def decline_call(
    call_id: int, request: Request, current_user: User = Depends(get_current_user),
) -> CallOut:
    service = _service(request)
    call = await _get_call(service, current_user, call_id)
    try:
        call = await service.decline(current_user.id, call)
    except NotCallee:
        raise HTTPException(status_code=403, detail="Only the person called can decline")
    except CallAlreadyActive:
        raise HTTPException(status_code=409, detail="Call is already active")
    return CallOut.of(call)


@router.post("/{call_id}/refresh", status_code=204)
async def refresh_call(
    call_id: int, request: Request, current_user: User = Depends(get_current_user),
) -> Response:
    """Re-read the call's room now. Clients call this after joining and after leaving."""
    service = _service(request)
    call = await _get_call(service, current_user, call_id)
    if call.state not in TERMINAL:
        service.refresh(call.id)
    return Response(status_code=204)
