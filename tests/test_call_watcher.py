"""How open calls end: the rules in decide, the watcher around them, the start-up
reconcile and the LiveKit adapter. The room source is a fake and the clock is injected."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from livekit import api
from livekit.protocol import models

from app.models.Call import Call, CallEndReason, CallState
from app.services.call_rooms import LiveKitCallRooms
from app.services.calls import CallLimits, CallService, Watch, decide
from tests.test_calls import (  # noqa: F401
    answered, calls, clock, frames, livekit_env, pair, rooms,
    sockets, started, stored, stored_message, text_of)
from tests.test_push import run

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CALLER, CALLEE, OTHER = 1, 2, 3
HOUR = 3600
MISSED_TIMEOUT = (CallState.MISSED, CallEndReason.TIMEOUT)
MISSED_CANCELLED = (CallState.MISSED, CallEndReason.CANCELLED)
ENDED = {reason: (CallState.ENDED, reason) for reason in CallEndReason}


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def ringing_call():
    return SimpleNamespace(
        state=CallState.RINGING, caller_id=CALLER, callee_id=CALLEE, created_at=T0,
        ring_expires_at=at(30), answered_at=None)


def active_call():
    return SimpleNamespace(
        state=CallState.ACTIVE, caller_id=CALLER, callee_id=CALLEE, created_at=T0,
        ring_expires_at=at(30), answered_at=T0)


@pytest.mark.parametrize("call, steps, expected", [
    (ringing_call, [(30, {CALLER})], MISSED_TIMEOUT),
    (ringing_call, [(0, {CALLER}), (1, set()), (10, set())], None),
    (ringing_call, [(0, {CALLER}), (1, set()), (11, set())], MISSED_CANCELLED),
    (ringing_call, [(0, {CALLER}), (1, set()), (9, {CALLER}), (12, set())], None),
    (ringing_call, [(1, set()), (29, set())], None),
    (ringing_call, [(5, None), (30, None)], MISSED_TIMEOUT),
    (active_call, [(1, {CALLER, CALLEE}), (5, set()), (15, set())], ENDED[CallEndReason.HANGUP]),
    (active_call, [(1, {CALLER, CALLEE}), (5, set()), (12, {CALLER, CALLEE}), (20, set())], None),
    (active_call, [(1, {CALLER, CALLEE}), (2, {CALLER}), (301, {CALLER})], None),
    (active_call, [(1, {CALLER, CALLEE}), (2, {CALLER}), (302, {CALLER})],
     ENDED[CallEndReason.ALONE]),
    (active_call, [(1, {CALLER}), (100, {CALLER, CALLEE}), (350, {CALLER})], None),
    (active_call, [(1, {CALLER}), (100, set()), (150, {CALLER}), (301, {CALLER})],
     ENDED[CallEndReason.ALONE]),
    (active_call, [(10, set()), (30, set())], ENDED[CallEndReason.ABANDONED]),
    (active_call, [(29, set())], None),
    (active_call, [(5, {CALLER, CALLEE}), (40, set())], None),
    (active_call, [(1, {CALLER, CALLEE}), (100, None)], None),
    (active_call, [(40, None)], None),
    (active_call, [(12 * HOUR, {CALLER, CALLEE})], ENDED[CallEndReason.EXPIRED]),
    (active_call, [(12 * HOUR, None)], ENDED[CallEndReason.EXPIRED]),
    (active_call, [(1, {OTHER}), (30, {OTHER})], ENDED[CallEndReason.ABANDONED]),
], ids=[
    "ring-timeout", "caller-gone-just-under-the-grace", "caller-gone-for-the-grace",
    "caller-back-inside-the-grace", "caller-never-seen", "unreadable-past-expiry",
    "hangup-after-the-grace", "reconnect-inside-the-grace", "alone-at-4:59", "alone-at-5:00",
    "both-back-resets-alone", "alone-survives-an-empty-moment", "abandoned-at-30s",
    "not-abandoned-at-29s", "not-abandoned-once-occupied", "unreadable-after-occupied",
    "unreadable-keeps-it-open", "cap", "cap-with-unreadable-room", "other-identities-ignored",
])
def test_decide(call, steps, expected):
    call, watch, decision = call(), Watch(), None
    for seconds, present in steps:
        present = None if present is None else frozenset(present)
        decision = decide(call, watch, present, at(seconds), CallLimits())

    assert decision == expected


# -- the watcher -------------------------------------------------------------

def test_tick_ends_an_empty_call_in_steps(calls, rooms, clock, pair, sockets):
    call = answered(pair)
    for ws in (sockets.a, sockets.b):
        frames(ws)
    rooms.occupants[call["id"]] = {pair.a["id"], pair.b["id"]}
    calls.tick()
    rooms.occupants[call["id"]] = set()
    calls.tick()
    clock.advance(seconds=9)
    calls.tick()
    assert stored(pair.a_client, call["id"]).state == "active"
    clock.advance(seconds=1)

    calls.tick()
    calls.drain()

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason, row.open_key) == ("ended", "hangup", None)
    assert rooms.deleted == [call["id"]]
    message = stored_message(pair.a_client, call)
    assert text_of(message.content) == "Call ended · 0:10"
    assert message.metadata["call"]["duration_s"] == 10 and message.edited_at is not None
    for ws in (sockets.a, sockets.b):
        (frame,) = frames(ws)
        assert frame["call"]["state"] == "ended" and frame["call"]["duration_s"] == 10
        assert frame["content"]["content"][0]["content"][0]["text"] == "Call ended · 0:10"


def test_decline_and_timeout_have_one_winner(client, calls, clock, pair, sockets):
    call = started(pair.a_client, pair.convo)
    for ws in (sockets.a, sockets.b):
        frames(ws)
    clock.advance(seconds=31)

    async def race():
        await asyncio.gather(
            calls.service._finish(
                call["id"], CallState.RINGING, CallState.DECLINED, CallEndReason.DECLINED),
            calls.service.tick())

    client.portal.call(race)

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason) in {("declined", "declined"), ("missed", "timeout")}
    for ws in (sockets.a, sockets.b):
        (frame,) = frames(ws)
        assert frame["type"] == "call_state"
        assert (frame["call"]["state"], frame["call"]["reason"]) == (row.state, row.reason)


def test_a_stale_read_is_ignored(client, calls, rooms, clock, pair):
    call = answered(pair)
    both = {pair.a["id"], pair.b["id"]}
    rooms.occupants[call["id"]] = both
    calls.tick()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_read():
            entered.set()
            await release.wait()
            return both

        rooms.occupants[call["id"]] = slow_read
        stale = asyncio.ensure_future(calls.service.check(call["id"]))
        await entered.wait()
        rooms.occupants[call["id"]] = set()
        await calls.service.check(call["id"])
        release.set()
        await stale
        clock.advance(seconds=10)
        await calls.service.check(call["id"])

    client.portal.call(scenario)

    assert stored(pair.a_client, call["id"]).reason == "hangup"


def test_a_call_that_vanished_is_forgotten_and_its_room_deleted(client, calls, rooms, pair):
    call = answered(pair)
    rooms.occupants[call["id"]] = {pair.a["id"], pair.b["id"]}
    calls.tick()
    run(client, Call.filter(id=call["id"]).delete())

    calls.tick()
    calls.tick()
    calls.drain()

    assert rooms.deleted == [call["id"]]


def test_tick_sleeps_until_the_ring_deadline(calls, clock, pair):
    assert calls.tick() == 3.0
    started(pair.a_client, pair.convo)
    assert calls.tick() == 3.0

    clock.advance(seconds=28)
    assert calls.tick() == pytest.approx(2.0, abs=0.01)

    clock.advance(seconds=1.99)
    assert calls.tick() == 0.05


def test_run_backs_off_on_failures_and_recovers():
    slept = []
    results = [RuntimeError] * 6 + [0.25, RuntimeError, 1.0]

    class Stop(Exception):
        pass

    async def sleep(delay):
        slept.append(delay)
        if len(slept) == len(results):
            raise Stop

    async def tick():
        result = results[len(slept)]
        if result is RuntimeError:
            raise RuntimeError("boom")
        return result

    async def scenario():
        service = CallService(None, None, sleep=sleep)
        service.tick = tick
        with pytest.raises(Stop):
            await service.run()

    asyncio.run(scenario())

    assert slept == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 0.25, 3.0, 1.0]


# -- start-up reconcile ------------------------------------------------------

BOOM = RuntimeError("livekit is down")


@pytest.mark.parametrize("answer, before, steps, expected", [
    (False, 31, [], ("missed", "timeout")),
    (False, 0, [(0, set()), (31, set())], ("missed", "timeout")),
    (True, 31, [(0, set()), (10, set())], ("ended", "hangup")),
    (True, 0, [(0, set()), (30, set())], ("ended", "abandoned")),
    (True, 12 * HOUR + 1, [], ("ended", "expired")),
    (False, 0, [(0, BOOM), (10, BOOM)], ("ringing", None)),
    (False, 0, [(0, BOOM), (31, BOOM)], ("missed", "timeout")),
    (True, 0, [(0, BOOM), (60, BOOM)], ("active", None)),
    (True, 0, [(0, BOOM), (13 * HOUR, BOOM)], ("ended", "expired")),
], ids=[
    "ringing-past-expiry", "ringing-keeps-its-stored-deadline", "active-kept-then-hangup",
    "recently-answered-abandoned-at-30s", "older-than-the-cap", "unreachable-keeps-ringing",
    "unreachable-still-times-out", "unreachable-keeps-active", "unreachable-still-expires",
])
def test_reconcile(client, calls, rooms, clock, pair, answer, before, steps, expected):
    call = answered(pair) if answer else started(pair.a_client, pair.convo)
    clock.advance(seconds=before)
    restarted = CallService(
        client.app.state.comms, rooms, clock=clock.now, sleep=clock.sleep)

    async def after_restart():
        await restarted.reconcile()
        for seconds, occupants in steps:
            clock.advance(seconds=seconds)
            rooms.occupants[call["id"]] = occupants
            await restarted.tick()

    try:
        client.portal.call(after_restart)
    finally:
        client.portal.call(restarted.aclose)

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason) == expected
    assert (row.open_key is None) == (expected[1] is not None)


# -- the LiveKit adapter -----------------------------------------------------

def not_found():
    return api.ServerError(api.ServerErrorCode.NOT_FOUND, "no such room", status=404)


def unavailable():
    return api.ServerError(api.ServerErrorCode.UNAVAILABLE, "down", status=503)


class StubApi:
    def __init__(self, error=None, listed=()):
        self.room = self
        self._error = error
        self._listed = listed

    async def list_participants(self, request):
        if self._error:
            raise self._error
        return SimpleNamespace(participants=list(self._listed))

    async def remove_participant(self, request):
        if self._error:
            raise self._error

    async def delete_room(self, request):
        if self._error:
            raise self._error


def adapter(monkeypatch, stub):
    rooms = LiveKitCallRooms("https://livekit.test", "key", "secret")
    monkeypatch.setattr(rooms, "_api", lambda: stub)
    return rooms


@pytest.mark.parametrize("method, args", [
    ("participants", (5,)), ("remove_participant", (5, 1)), ("delete_room", (5,))])
def test_livekit_adapter_treats_a_missing_room_as_empty(monkeypatch, method, args):
    rooms = adapter(monkeypatch, StubApi(not_found()))

    result = asyncio.run(getattr(rooms, method)(*args))

    assert not result


@pytest.mark.parametrize("method, args", [
    ("participants", (5,)), ("remove_participant", (5, 1)), ("delete_room", (5,))])
def test_livekit_adapter_raises_other_errors(monkeypatch, method, args):
    rooms = adapter(monkeypatch, StubApi(unavailable()))

    with pytest.raises(api.ServerError):
        asyncio.run(getattr(rooms, method)(*args))


def test_livekit_adapter_lists_connected_users(monkeypatch):
    state = models.ParticipantInfo.State
    listed = [
        models.ParticipantInfo(identity="1", state=state.ACTIVE),
        models.ParticipantInfo(identity="2", state=state.JOINED),
        models.ParticipantInfo(identity="3", state=state.DISCONNECTED),
        models.ParticipantInfo(identity="not-a-user", state=state.ACTIVE),
    ]
    rooms = adapter(monkeypatch, StubApi(listed=listed))

    assert asyncio.run(rooms.participants(5)) == frozenset({1, 2})
