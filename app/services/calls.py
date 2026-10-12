import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.models.Call import Call, CallEndReason, CallState
from app.models.Message import Message
from app.models.User import User
from app.services import read_state
from app.services.call_rooms import CallRooms, create_call_rooms
from app.services.message_content import normalize_content, serialize
from app.services.push import CALL_PUSH_TTL_SECONDS, call_payload, call_tag, push, read_payload

logger = logging.getLogger("app.services.calls")

RINGING, ACTIVE = CallState.RINGING, CallState.ACTIVE
TERMINAL = frozenset({CallState.ENDED, CallState.MISSED, CallState.DECLINED})
CALL_SOURCES = ["microphone", "screen_share", "screen_share_audio"]


@dataclass(frozen=True)
class CallLimits:
    ring: timedelta = timedelta(seconds=30)
    poll: float = 3.0
    grace: timedelta = timedelta(seconds=10)
    alone: timedelta = timedelta(minutes=5)
    abandoned: timedelta = timedelta(seconds=30)
    cap: timedelta = timedelta(hours=12)
    unanswered_max: int = 3
    unanswered_window: timedelta = timedelta(minutes=10)
    max_backoff: float = 60.0


class CallNotFound(Exception):
    pass


class CallEnded(Exception):
    pass


class CallAlreadyActive(Exception):
    pass


class NotCallee(Exception):
    pass


class TooManyCalls(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


def format_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def duration_s(call: Call) -> int | None:
    if call.answered_at is None or call.ended_at is None:
        return None
    return max(0, int((call.ended_at - call.answered_at).total_seconds()))


def fallback_text(call: Call) -> str:
    match call.state:
        case CallState.RINGING:
            return "Started a call"
        case CallState.ACTIVE:
            return "Call in progress"
        case CallState.ENDED:
            seconds = duration_s(call)
            return "Call ended" if seconds is None else f"Call ended · {format_duration(seconds)}"
        case CallState.MISSED:
            return "Missed call"
        case _:
            return "Call declined"


def fallback_doc(call: Call) -> dict:
    return normalize_content(fallback_text(call), allow_empty=False)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def call_meta(call: Call) -> dict:
    return {
        "id": call.id,
        "state": call.state,
        "reason": call.reason,
        "started_at": _iso(call.created_at),
        "answered_at": _iso(call.answered_at),
        "ended_at": _iso(call.ended_at),
        "duration_s": duration_s(call),
    }


def call_json(call: Call) -> dict:
    """The CallOut of the API and the frames. Needs ``call.message`` loaded."""
    return {
        "id": call.id,
        "conversation_id": call.conversation_id,
        "message_id": str(call.message.uuid),
        "caller_id": call.caller_id,
        "callee_id": call.callee_id,
        "state": call.state,
        "reason": call.reason,
        "created_at": _iso(call.created_at),
        "ring_expires_at": _iso(call.ring_expires_at),
        "answered_at": _iso(call.answered_at),
        "ended_at": _iso(call.ended_at),
        "duration_s": duration_s(call),
    }


@dataclass
class Watch:
    """What the watcher has seen of one open call. Kept in memory only."""

    caller_seen: bool = False
    caller_gone_since: datetime | None = None
    ever_occupied: bool = False
    empty_since: datetime | None = None
    alone_since: datetime | None = None

    @classmethod
    def resumed(cls, call: Call, now: datetime, limits: CallLimits) -> "Watch":
        """A watch for a call found after a restart: one answered long ago was
        occupied at some point, so it is not judged abandoned."""
        answered = call.state == ACTIVE and now - call.answered_at >= limits.abandoned
        return cls(ever_occupied=answered)


def decide(
    call: Call, watch: Watch, present: frozenset[int] | None, now: datetime, limits: CallLimits,
) -> tuple[CallState, CallEndReason] | None:
    """The transition an open call is due for, given who is in its room now
    (None when LiveKit could not be read). Updates *watch*."""
    if call.state == RINGING:
        if now >= call.ring_expires_at:
            return CallState.MISSED, CallEndReason.TIMEOUT
        if present is None:
            return None
        if call.caller_id in present:
            watch.caller_seen, watch.caller_gone_since = True, None
        elif watch.caller_seen:
            watch.caller_gone_since = watch.caller_gone_since or now
            if now - watch.caller_gone_since >= limits.grace:
                return CallState.MISSED, CallEndReason.CANCELLED
        return None

    if now - call.created_at >= limits.cap:
        return CallState.ENDED, CallEndReason.EXPIRED
    if present is None:
        return None
    inside = len(present & {call.caller_id, call.callee_id})
    if inside:
        watch.ever_occupied, watch.empty_since = True, None
    if inside >= 2:
        watch.alone_since = None
    elif inside == 1:
        # Kept through an empty moment, so a reconnect flap can't restart the clock.
        watch.alone_since = watch.alone_since or now
        if now - watch.alone_since >= limits.alone:
            return CallState.ENDED, CallEndReason.ALONE
    elif not watch.ever_occupied:
        if now - call.answered_at >= limits.abandoned:
            return CallState.ENDED, CallEndReason.ABANDONED
    else:
        watch.empty_since = watch.empty_since or now
        if now - watch.empty_since >= limits.grace:
            return CallState.ENDED, CallEndReason.HANGUP
    return None


def _utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _loaded(call: Call | None) -> Call | None:
    # SQLite hands datetimes back naive; the clock and the rules are aware.
    if call is not None:
        for name in ("created_at", "ring_expires_at", "answered_at", "ended_at"):
            setattr(call, name, _utc(getattr(call, name)))
    return call


def _with_message():
    return Call.all().select_related("message")


class CallService:
    """Runs DM calls: the state machine, the watcher over their LiveKit rooms,
    and the frames and pushes that follow each transition.

    Every transition of a call happens under that call's lock and as a
    compare-and-set on its state, so racing paths (a decline and the ring
    timeout, say) produce exactly one winner. State here is in memory and
    single-process, like the voice presence poller.
    """

    def __init__(
        self,
        comms,
        rooms: CallRooms | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep=asyncio.sleep,
        limits: CallLimits = CallLimits(),
    ) -> None:
        self._comms = comms
        self._rooms = rooms
        self._clock = clock
        self._sleep = sleep
        self.limits = limits
        self._locks: dict[int, asyncio.Lock] = {}
        self._watch: dict[int, Watch] = {}
        # Every read of a room takes a number when it starts; a call only accepts
        # a read that started after the one it last applied.
        self._seq = 0
        self._applied: dict[int, int] = {}
        self._ring_pushed: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._run_task: asyncio.Task | None = None
        self._refreshing: set[int] = set()
        self._refresh_again: set[int] = set()
        self._closed = False

    def _now(self) -> datetime:
        return self._clock()

    def _lock(self, call_id: int) -> asyncio.Lock:
        return self._locks.setdefault(call_id, asyncio.Lock())

    # -- reads ---------------------------------------------------------------

    async def _reload(self, call_id: int) -> Call:
        return _loaded(await _with_message().get(id=call_id))

    async def get_for(self, user_id: int, call_id: int) -> Call:
        call = _loaded(await _with_message().get_or_none(id=call_id))
        if call is None or user_id not in (call.caller_id, call.callee_id):
            raise CallNotFound
        return call

    async def active_for(self, user_id: int) -> list[Call]:
        rows = await _with_message().filter(
            Q(caller_id=user_id) | Q(callee_id=user_id), open_key__isnull=False)
        calls = [await self._expire_if_due(_loaded(call)) for call in rows]
        return [call for call in calls if call.state not in TERMINAL]

    async def _expire_if_due(self, call: Call) -> Call:
        if call.state == RINGING and self._now() >= call.ring_expires_at:
            await self._finish(call.id, RINGING, CallState.MISSED, CallEndReason.TIMEOUT)
            return await self._reload(call.id)
        return call

    # -- the transition core -------------------------------------------------

    async def _cas(
        self, call_id: int, expected: CallState, to: CallState,
        reason: CallEndReason | None = None,
    ) -> Call | None:
        """Move a call from *expected* to *to*, rewriting its message in the
        same transaction, then send the frames. Needs the call's lock.
        None if the call was no longer in *expected*."""
        now = self._now()
        values: dict = {"state": to.value}
        if to == ACTIVE:
            values["answered_at"] = now
        if to in TERMINAL:
            values |= {"reason": reason.value, "ended_at": now, "open_key": None}
        async with in_transaction():
            if not await Call.filter(id=call_id, state=expected.value).update(**values):
                return None
            call = _loaded(await _with_message().get(id=call_id))
            await Message.filter(id=call.message_id).update(
                content=serialize(fallback_doc(call)),
                metadata={"call": call_meta(call)},
                **({"edited_at": now} if to in TERMINAL else {}))
        await self._send_state(call)
        if to == ACTIVE:
            self._watch[call_id] = Watch()
        if to in TERMINAL:
            self._watch.pop(call_id, None)
            self._applied.pop(call_id, None)
        return call

    async def _send_state(self, call: Call) -> None:
        await self._send(call, {
            "type": "call_state", "call": call_json(call), "content": fallback_doc(call)})

    async def _send(self, call: Call, frame: dict) -> None:
        if self._comms is None:
            return
        try:
            await self._comms.send_to_users([call.caller_id, call.callee_id], frame)
        except Exception:
            logger.warning("Failed to send %s for call %s", frame["type"], call.id, exc_info=True)

    async def _answer(self, call_id: int) -> Call | None:
        async with self._lock(call_id):
            call = await self._cas(call_id, RINGING, ACTIVE)
        if call is not None:
            await self._after_answer(call)
        return call

    async def _after_answer(self, call: Call) -> None:
        """Best effort: the answer already stands."""
        try:
            conversation_id = call.conversation_id
            moved = await read_state.advance(
                call.callee_id, conversation_id=conversation_id, message_pk=call.message_id)
            if moved and self._comms is not None:
                marker = await read_state.get_marker(
                    call.callee_id, conversation_id=conversation_id)
                last_read = await read_state.resolve_marker_uuid(
                    marker, conversation_id=conversation_id)
                await self._comms.send_to_user(call.callee_id, {
                    "type": "read_state",
                    "channel_id": None,
                    "server_id": None,
                    "conversation_id": conversation_id,
                    "last_read_message_id": last_read,
                })
        except Exception:
            logger.warning("Failed to move the read marker for call %s", call.id, exc_info=True)
        self._close_push(call)

    async def _finish(
        self, call_id: int, expected: CallState, to: CallState, reason: CallEndReason,
    ) -> Call | None:
        """End a call: the row and its message first, then the frames, and only
        then the room and the push, so neither waits on the other services."""
        async with self._lock(call_id):
            call = await self._cas(call_id, expected, to, reason)
            if call is not None:
                self._locks.pop(call_id, None)
        if call is None:
            return None
        self._spawn(self._delete_room(call_id))
        if to == CallState.MISSED:
            # The "Incoming call" entry stays as the trace of the missed call.
            self._ring_pushed.discard(call_id)
        else:
            self._close_push(call)
        return call

    def _close_push(self, call: Call, *, force: bool = False) -> None:
        if call.id not in self._ring_pushed and not force:
            return
        self._ring_pushed.discard(call.id)
        tag = call_tag(call.id)
        # The read push skips Apple, which revokes subscriptions that show nothing.
        push.schedule(push.send_to_user(
            call.callee_id, read_payload(tag), topic=tag, urgency="normal", skip_apple=True))

    async def _delete_room(self, call_id: int) -> None:
        if self._rooms is None:
            return
        try:
            await self._rooms.delete_room(call_id)
        except Exception:
            logger.debug("Could not delete the room of call %s", call_id, exc_info=True)

    # -- endpoints -----------------------------------------------------------

    async def start(self, user_id: int, conversation) -> Call:
        """Start a call in *conversation*, or return the one already open there.
        If the open call is ringing for this user, starting it answers it."""
        other_id = conversation.other_user_id(user_id)
        for attempt in (1, 2):
            open_call = _loaded(
                await _with_message().get_or_none(open_key=conversation.id))
            if open_call is not None:
                open_call = await self._expire_if_due(open_call)
                if open_call.state not in TERMINAL:
                    return await self._join_open(open_call, user_id)
            await self._check_unanswered_cap(user_id, other_id)
            try:
                call = await self._create(user_id, other_id, conversation.id)
            except IntegrityError:
                # A concurrent start won; that call is the open one now.
                if attempt == 2:
                    raise
                continue
            break

        try:
            await read_state.advance(
                user_id, conversation_id=conversation.id, message_pk=call.message_id)
        except Exception:
            logger.warning("Failed to move the read marker for call %s", call.id, exc_info=True)
        async with self._lock(call.id):
            await self._send(call, {
                "type": "direct_message",
                "id": str(call.message.uuid),
                "conversation_id": conversation.id,
                "user_id": user_id,
                "content": fallback_doc(call),
                "timestamp": call.created_at.isoformat(),
                "attachments": [],
                "reactions": [],
                "embeds": [],
                "reply_to": None,
                "call": call_meta(call),
            })
            await self._send_state(call)
        self._watch[call.id] = Watch()
        if push.enabled and not self._callee_active(other_id):
            push.schedule(self._push_ring(call, user_id))
        return call

    def _callee_active(self, user_id: int) -> bool:
        return self._comms is not None and self._comms.connection_manager.is_active(user_id)

    async def _join_open(self, call: Call, user_id: int) -> Call:
        if call.state == RINGING and user_id == call.callee_id:
            answered = await self._answer(call.id)
            return answered or await self._reload(call.id)
        return call

    async def _check_unanswered_cap(self, caller_id: int, callee_id: int) -> None:
        limits = self.limits
        now = self._now()
        since = now - limits.unanswered_window
        rows = await Call.filter(
            caller_id=caller_id, callee_id=callee_id, answered_at__isnull=True,
            created_at__gte=since).order_by("created_at")
        if len(rows) >= limits.unanswered_max:
            oldest = _utc(rows[len(rows) - limits.unanswered_max].created_at)
            wait = (oldest + limits.unanswered_window - now).total_seconds()
            raise TooManyCalls(max(1, math.ceil(wait)))

    async def _create(self, caller_id: int, callee_id: int, conversation_id: int) -> Call:
        now = self._now()
        async with in_transaction():
            message = await Message.create(
                uuid=uuid4(), author_id=caller_id, conversation_id=conversation_id,
                timestamp=now, content=serialize(normalize_content(
                    "Started a call", allow_empty=False)),
                metadata={})
            call = await Call.create(
                conversation_id=conversation_id, caller_id=caller_id, callee_id=callee_id,
                message_id=message.id, state=RINGING.value, created_at=now,
                ring_expires_at=now + self.limits.ring, open_key=conversation_id)
            await Message.filter(id=message.id).update(metadata={"call": call_meta(call)})
        return await self._reload(call.id)

    async def _push_ring(self, call: Call, caller_id: int) -> None:
        if (await Call.get(id=call.id)).state != RINGING:
            return
        caller = await User.get(id=caller_id)
        tag = call_tag(call.id)
        self._ring_pushed.add(call.id)
        await push.send_to_user(
            call.callee_id,
            call_payload(
                call_id=call.id, conversation_id=call.conversation_id,
                caller_name=caller.username, message_id=call.message_id,
                message_uuid=str(call.message.uuid)),
            topic=tag, urgency="high", ttl=CALL_PUSH_TTL_SECONDS)
        # The push raced an answer or a decline whose close went out first.
        state = (await Call.get(id=call.id)).state
        if state not in (RINGING, CallState.MISSED):
            self._close_push(call, force=True)

    async def take_token(self, user_id: int, call: Call) -> Call:
        """Taking a token is how the callee answers a ringing call. Raises
        CallEnded once the call is over."""
        call = await self._expire_if_due(call)
        if call.state in TERMINAL:
            raise CallEnded
        if call.state == RINGING and user_id == call.callee_id:
            answered = await self._answer(call.id)
            if answered is not None:
                return answered
            call = await self._reload(call.id)
            if call.state in TERMINAL:
                raise CallEnded
        return call

    async def decline(self, user_id: int, call: Call) -> Call:
        while True:
            call = await self._expire_if_due(call)
            if call.state in TERMINAL:
                return call
            if user_id != call.callee_id:
                raise NotCallee
            if call.state == ACTIVE:
                raise CallAlreadyActive
            declined = await self._finish(
                call.id, RINGING, CallState.DECLINED, CallEndReason.DECLINED)
            if declined is not None:
                return declined
            call = await self._reload(call.id)

    async def leave_rooms(self, user_id: int, *, except_call_id: int | None = None) -> None:
        """Take the user out of the rooms of their open calls, best effort."""
        if self._rooms is None:
            return
        query = Call.filter(Q(caller_id=user_id) | Q(callee_id=user_id), open_key__isnull=False)
        if except_call_id is not None:
            query = query.exclude(id=except_call_id)
        for call_id in await query.values_list("id", flat=True):
            try:
                await self._rooms.remove_participant(call_id, user_id)
            except Exception:
                logger.debug(
                    "Could not remove user %s from call %s", user_id, call_id, exc_info=True)
                continue
            self.refresh(call_id)

    async def end_for_logout(self, user_id: int) -> None:
        """The user's last session is gone: end their active call and cancel the
        ones they are ringing. A call they are being rung for is left alone, as
        it may still be answered on a phone."""
        for call in await Call.filter(
                Q(caller_id=user_id) | Q(callee_id=user_id), open_key__isnull=False):
            if call.state == ACTIVE:
                await self._finish(call.id, ACTIVE, CallState.ENDED, CallEndReason.HANGUP)
            elif call.state == RINGING and call.caller_id == user_id:
                await self._finish(call.id, RINGING, CallState.MISSED, CallEndReason.CANCELLED)

    # -- watcher -------------------------------------------------------------

    async def reconcile(self) -> None:
        """Pick up the open calls after a start-up. LiveKit is never read here,
        so an unreachable LiveKit can't close anything."""
        await Call.filter(
            open_key__isnull=False, state__in=[s.value for s in TERMINAL]).update(open_key=None)
        now = self._now()
        for call in await Call.filter(open_key__isnull=False):
            call = _loaded(call)
            if call.state == RINGING and now >= call.ring_expires_at:
                await self._finish(call.id, RINGING, CallState.MISSED, CallEndReason.TIMEOUT)
            elif call.state == ACTIVE and now - call.created_at >= self.limits.cap:
                await self._finish(call.id, ACTIVE, CallState.ENDED, CallEndReason.EXPIRED)
            else:
                self._watch[call.id] = Watch.resumed(call, now, self.limits)

    async def check(self, call_id: int) -> None:
        """Read the call's room and apply whatever transition is due."""
        self._seq += 1
        seq = self._seq
        present = None
        if self._rooms is not None:
            try:
                present = await self._rooms.participants(call_id)
            except Exception:
                logger.debug("Could not read the room of call %s", call_id, exc_info=True)
        async with self._lock(call_id):
            if seq <= self._applied.get(call_id, 0):
                return
            self._applied[call_id] = seq
            call = _loaded(await Call.get_or_none(id=call_id))
            if call is None or call.state in TERMINAL:
                self._watch.pop(call_id, None)
                return
            now = self._now()
            watch = self._watch.setdefault(call_id, Watch.resumed(call, now, self.limits))
            decision = decide(call, watch, present, now, self.limits)
        if decision:
            await self._finish(call_id, CallState(call.state), *decision)

    async def tick(self) -> float:
        """Check every open call once. Returns how long to sleep before the next."""
        rows = await Call.filter(open_key__isnull=False).values_list(
            "id", "state", "ring_expires_at")
        for gone in self._watch.keys() - {row[0] for row in rows}:
            # Cascaded away with its conversation or a user.
            self._watch.pop(gone, None)
            self._applied.pop(gone, None)
            self._spawn(self._delete_room(gone))
        await asyncio.gather(*(self.check(row[0]) for row in rows))
        now = self._now()
        next_ring = min(
            (_utc(row[2]) for row in rows if row[1] == RINGING.value), default=None)
        if next_ring is None:
            return self.limits.poll
        return min(self.limits.poll, max(0.05, (next_ring - now).total_seconds()))

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                delay = await self.tick()
            except Exception:
                if failures == 0:
                    logger.warning("Call watcher failed", exc_info=True)
                else:
                    logger.debug("Call watcher failed again", exc_info=True)
                delay = min(self.limits.poll * 2 ** min(failures, 30), self.limits.max_backoff)
                failures += 1
            else:
                if failures:
                    logger.info("Call watcher recovered after %s failure(s)", failures)
                    failures = 0
            await self._sleep(delay)

    def start_watcher(self) -> None:
        if self._run_task is None and not self._closed:
            self._run_task = asyncio.create_task(self.run())

    def refresh(self, call_id: int) -> None:
        """Read the call's room soon. Calls while a read is in flight fold into
        one follow-up read."""
        if call_id in self._refreshing:
            self._refresh_again.add(call_id)
            return
        self._refreshing.add(call_id)
        self._spawn(self._refresh_loop(call_id))

    async def _refresh_loop(self, call_id: int) -> None:
        try:
            while True:
                self._refresh_again.discard(call_id)
                await self.check(call_id)
                if call_id not in self._refresh_again:
                    return
        finally:
            self._refreshing.discard(call_id)

    # -- tasks ---------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Background call task failed", exc_info=task.exception())

    async def drain(self) -> None:
        """Wait for every spawned task to finish (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks)
        if self._run_task is not None:
            tasks.append(self._run_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._rooms is not None:
            await self._rooms.aclose()


def create_call_service(comms) -> CallService:
    return CallService(comms, create_call_rooms())
