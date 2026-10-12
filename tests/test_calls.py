"""DM calls over the API: start, token, decline, active, logout, the wire and the pushes.

The room source is a fake and the clock is injected, so nothing here waits.
The watcher's own rules are in test_call_watcher.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.Call import Call, CallState
from app.models.Conversation import Conversation
from app.models.Message import Message
from app.routers import voice
from app.services.calls import CallService, fallback_text
from tests.conftest import ORIGIN, PASSWORD, create_channel, create_server, register, ws_ready
from tests.test_direct_messages import dm_frame, open_conversation
from tests.test_friends import fresh_limits  # noqa: F401
from tests.test_message_changes import changes_since, cursor_of
from tests.test_push import APPLE, drain as drain_push, push_on, report, run, subscribe, vapid_env  # noqa: F401
from tests.test_voice_token import _claims, _in_voice, _presence_with, livekit_env  # noqa: F401

HEADERS = {"origin": ORIGIN}
CALL_FRAMES = {"direct_message", "call_state", "read_state"}


def text_doc(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": text}]}]}


def text_of(content):
    return json.loads(content)["content"][0]["content"][0]["text"]


class FakeClock:
    def __init__(self):
        self.current = datetime.now(timezone.utc)

    def now(self):
        return self.current

    def advance(self, **delta):
        self.current += timedelta(**delta)

    async def sleep(self, delay):
        await asyncio.sleep(0)


class FakeRooms:
    """Who is in each call's room. A value may be an exception to raise, or an
    async callable to wait in."""

    def __init__(self):
        self.occupants = {}
        self.reads = []
        self.removed = []
        self.deleted = []

    async def participants(self, call_id):
        self.reads.append(call_id)
        value = self.occupants.get(call_id, set())
        if callable(value):
            value = await value()
        if isinstance(value, Exception):
            raise value
        return frozenset(value)

    async def remove_participant(self, call_id, user_id):
        self.removed.append((call_id, user_id))

    async def delete_room(self, call_id):
        self.deleted.append(call_id)

    async def aclose(self):
        pass


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def rooms():
    return FakeRooms()


@pytest.fixture
def calls(client, clock, rooms, livekit_env, monkeypatch):  # noqa: F811
    """The app's call service, swapped for one on the fake clock and rooms."""
    client.portal.call(client.app.state.calls.aclose)
    service = CallService(
        client.app.state.comms, rooms, clock=clock.now, sleep=clock.sleep)
    monkeypatch.setattr(client.app.state, "calls", service)
    yield SimpleNamespace(
        service=service,
        tick=lambda: client.portal.call(service.tick),
        drain=lambda: client.portal.call(service.drain),
        end_active=lambda user_id: client.portal.call(service.end_for_logout, user_id))
    client.portal.call(service.aclose)


@pytest.fixture
def pair(client, new_client):
    alice = register(client, "alice-call")
    bob_client = new_client()
    bob = register(bob_client, "bob-call")
    convo = open_conversation(client, bob["id"])
    return SimpleNamespace(
        a_client=client, a=alice, b_client=bob_client, b=bob, convo=convo["id"],
        client_of={"caller": client, "callee": bob_client})


@pytest.fixture
def sockets(pair):
    with pair.a_client.websocket_connect("/ws", headers=HEADERS) as a_ws, \
            pair.b_client.websocket_connect("/ws", headers=HEADERS) as b_ws:
        ws_ready(a_ws)
        ws_ready(b_ws)
        yield SimpleNamespace(a=a_ws, b=b_ws)


def frames(ws):
    """The call frames this socket has received since the last time it was asked."""
    ws.send_json({"type": "connection_init"})
    got = []
    while (frame := ws.receive_json())["type"] != "presence_init":
        got.append(frame)
    return [f for f in got if f["type"] in CALL_FRAMES]


def start_call(user_client, conversation_id):
    return user_client.post("/api/calls", json={"conversation_id": conversation_id})


def started(user_client, conversation_id):
    res = start_call(user_client, conversation_id)
    assert res.status_code == 200, res.text
    return res.json()


def take_token(user_client, call_id):
    return user_client.post(f"/api/calls/{call_id}/token")


def decline(user_client, call_id):
    return user_client.post(f"/api/calls/{call_id}/decline")


def active_ids(user_client):
    res = user_client.get("/api/calls/active")
    assert res.status_code == 200, res.text
    return [c["id"] for c in res.json()["calls"]]


def stored(client, call_id):
    return run(client, Call.get(id=call_id))


def stored_message(client, call):
    return run(client, Message.get(uuid=call["message_id"]))


def conversation_row(user_client, conversation_id):
    rows = user_client.get("/conversations/").json()
    return next(row for row in rows if row["id"] == conversation_id)


def answered(pair):
    call = started(pair.a_client, pair.convo)
    assert take_token(pair.b_client, call["id"]).status_code == 200
    return call


def timed_out(pair, clock, calls):
    call = started(pair.a_client, pair.convo)
    clock.advance(seconds=31)
    calls.tick()
    return call


def ended(pair, calls):
    call = answered(pair)
    calls.end_active(pair.a["id"])
    return call


# -- start -------------------------------------------------------------------

def test_start_creates_row_message_and_frames_in_order(calls, pair, sockets):
    call = started(pair.a_client, pair.convo)

    assert call["state"] == "ringing"
    assert (call["caller_id"], call["callee_id"]) == (pair.a["id"], pair.b["id"])
    for ws in (sockets.a, sockets.b):
        message, state = frames(ws)
        assert message["type"] == "direct_message"
        assert message["id"] == call["message_id"]
        assert message["user_id"] == pair.a["id"]
        assert message["content"] == text_doc("Started a call")
        assert message["call"] == stored_message(pair.a_client, call).metadata["call"]
        assert state == {"type": "call_state", "call": call, "content": text_doc("Started a call")}

    row = stored(pair.a_client, call["id"])
    assert row.open_key == pair.convo
    message = stored_message(pair.a_client, call)
    assert message.author_id == pair.a["id"] and message.conversation_id == pair.convo
    assert message.metadata["call"]["state"] == "ringing"
    assert conversation_row(pair.a_client, pair.convo)["unread_count"] == 0
    assert conversation_row(pair.b_client, pair.convo)["unread_count"] == 1
    assert active_ids(pair.a_client) == active_ids(pair.b_client) == [call["id"]]


@pytest.mark.parametrize("state, who", [
    ("ringing", "caller"), ("active", "caller"), ("active", "callee")])
def test_start_again_returns_the_same_call(calls, pair, sockets, state, who):
    call = started(pair.a_client, pair.convo)
    if state == "active":
        assert take_token(pair.b_client, call["id"]).status_code == 200
    for ws in (sockets.a, sockets.b):
        frames(ws)

    again = started(pair.client_of[who], pair.convo)

    assert again["id"] == call["id"] and again["state"] == state
    assert frames(sockets.a) == frames(sockets.b) == []


def test_crossing_start_answers(calls, pair, sockets):
    call = started(pair.a_client, pair.convo)
    for ws in (sockets.a, sockets.b):
        frames(ws)

    crossed = started(pair.b_client, pair.convo)

    assert crossed["id"] == call["id"] and crossed["state"] == "active"
    assert crossed["answered_at"] is not None
    assert [f["type"] for f in frames(sockets.a)] == ["call_state"]
    callee_frames = frames(sockets.b)
    assert [f["type"] for f in callee_frames] == ["call_state", "read_state"]
    assert callee_frames[1]["last_read_message_id"] == call["message_id"]


def test_concurrent_starts_make_one_call(client, calls, pair):
    async def both():
        conversation = await Conversation.get(id=pair.convo)
        return await asyncio.gather(
            calls.service.start(pair.a["id"], conversation),
            calls.service.start(pair.b["id"], conversation))

    first, second = client.portal.call(both)

    assert first.id == second.id
    assert run(client, Call.all().count()) == 1
    assert run(client, Message.filter(conversation_id=pair.convo).count()) == 1


@pytest.mark.parametrize("outsider", [False, True], ids=["unknown-conversation", "outsider"])
def test_start_404(calls, pair, new_client, outsider):
    carol = new_client()
    register(carol, "carol-call")
    res = start_call(carol, pair.convo) if outsider else start_call(pair.a_client, 99999)
    assert res.status_code == 404


def test_start_503_writes_nothing(client, calls, pair, monkeypatch):
    monkeypatch.setattr(voice, "LIVEKIT_URL", "")

    assert start_call(pair.a_client, pair.convo).status_code == 503

    assert run(client, Call.all().count()) == 0
    assert run(client, Message.all().count()) == 0


def test_start_after_expiry_makes_a_new_call(calls, clock, pair):
    first = started(pair.a_client, pair.convo)
    clock.advance(seconds=31)

    second = started(pair.a_client, pair.convo)

    assert second["id"] != first["id"] and second["state"] == "ringing"
    old = stored(pair.a_client, first["id"])
    assert (old.state, old.reason, old.open_key) == ("missed", "timeout", None)


def miss(pair, clock):
    started(pair.a_client, pair.convo)
    clock.advance(seconds=31)


def answer_then_end(pair, calls):
    answered(pair)
    calls.end_active(pair.a["id"])


def decline_it(pair, clock):
    call = started(pair.a_client, pair.convo)
    assert decline(pair.b_client, call["id"]).status_code == 200


@pytest.mark.parametrize("earlier, wait, blocked, retry_after", [
    (["miss", "miss", "miss"], timedelta(0), True, 507),
    (["answer", "miss", "miss"], timedelta(0), False, None),
    (["decline", "miss", "miss"], timedelta(0), True, 538),
    (["miss", "miss", "miss"], timedelta(minutes=10), False, None),
], ids=["three-unanswered", "answered-do-not-count", "declined-count", "window-slides"])
def test_unanswered_cap(calls, clock, pair, earlier, wait, blocked, retry_after):
    for outcome in earlier:
        if outcome == "miss":
            miss(pair, clock)
        elif outcome == "answer":
            answer_then_end(pair, calls)
        else:
            decline_it(pair, clock)
    clock.advance(seconds=wait.total_seconds())

    res = start_call(pair.a_client, pair.convo)

    if blocked:
        assert res.status_code == 429
        assert res.headers["Retry-After"] == str(retry_after)
    else:
        assert res.status_code == 200


def test_unanswered_cap_is_per_callee(calls, clock, pair, new_client):
    carol_client = new_client()
    carol = register(carol_client, "carol-cap")
    for _ in range(3):
        miss(pair, clock)
    other = open_conversation(pair.a_client, carol["id"])

    assert start_call(pair.a_client, other["id"]).status_code == 200


def test_start_rate_limit(calls, pair, fresh_limits, monkeypatch):  # noqa: F811
    monkeypatch.setenv("START_CALL_RATE_LIMIT", "2/minute")

    statuses = [start_call(pair.a_client, pair.convo).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    assert start_call(pair.b_client, pair.convo).status_code == 200


# -- token -------------------------------------------------------------------

@pytest.mark.parametrize("who", ["caller", "callee"])
def test_token_claims(calls, pair, who):
    call = started(pair.a_client, pair.convo)
    user = pair.a if who == "caller" else pair.b

    res = take_token(pair.client_of[who], call["id"])

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["room"] == f"call_{call['id']}" and body["url"] == "wss://livekit.test"
    claims = _claims(body["token"])
    assert claims["sub"] == str(user["id"]) and claims["name"] == user["username"]
    assert isinstance(json.loads(claims["metadata"]), dict)
    assert claims["exp"] - claims["nbf"] == 300
    grant = claims["video"]
    assert grant["room"] == body["room"] and grant["roomJoin"] is True
    assert grant["canPublish"] is True and grant["canSubscribe"] is True
    assert grant["canUpdateOwnMetadata"] is True
    assert set(grant["canPublishSources"]) == {"microphone", "screen_share", "screen_share_audio"}


def test_token_for_an_outsider_or_unknown_call_is_404(calls, pair, new_client):
    call = started(pair.a_client, pair.convo)
    carol = new_client()
    register(carol, "carol-token")

    assert take_token(carol, call["id"]).status_code == 404
    assert take_token(pair.a_client, 99999).status_code == 404
    assert active_ids(carol) == []


def test_token_503_changes_nothing(calls, pair, monkeypatch):
    call = started(pair.a_client, pair.convo)
    monkeypatch.setattr(voice, "LIVEKIT_URL", "")

    assert take_token(pair.b_client, call["id"]).status_code == 503

    assert stored(pair.a_client, call["id"]).state == "ringing"


@pytest.mark.parametrize("how", ["declined", "missed", "ended"])
def test_token_409_when_terminal(calls, clock, pair, how):
    if how == "declined":
        call = started(pair.a_client, pair.convo)
        assert decline(pair.b_client, call["id"]).status_code == 200
    elif how == "missed":
        call = timed_out(pair, clock, calls)
    else:
        call = ended(pair, calls)

    for client in (pair.a_client, pair.b_client):
        res = take_token(client, call["id"])
        assert res.status_code == 409 and res.json()["detail"] == "Call has ended"


def test_token_after_the_ring_expired_is_409(calls, clock, pair):
    call = started(pair.a_client, pair.convo)
    clock.advance(seconds=31)

    assert take_token(pair.b_client, call["id"]).status_code == 409

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason) == ("missed", "timeout")


def test_callee_token_answers(calls, pair, sockets):
    call = started(pair.a_client, pair.convo)
    for ws in (sockets.a, sockets.b):
        frames(ws)

    assert take_token(pair.b_client, call["id"]).status_code == 200

    row = stored(pair.a_client, call["id"])
    assert row.state == "active" and row.answered_at is not None
    message = stored_message(pair.a_client, call)
    assert text_of(message.content) == "Call in progress"
    assert message.metadata["call"]["state"] == "active"
    assert message.edited_at is None
    assert [f["type"] for f in frames(sockets.a)] == ["call_state"]
    callee_frames = frames(sockets.b)
    assert [f["type"] for f in callee_frames] == ["call_state", "read_state"]
    assert callee_frames[0]["call"]["state"] == "active"
    assert callee_frames[0]["content"] == text_doc("Call in progress")
    assert conversation_row(pair.b_client, pair.convo)["unread_count"] == 0


def test_caller_token_does_not_answer(calls, pair):
    call = started(pair.a_client, pair.convo)

    assert take_token(pair.a_client, call["id"]).status_code == 200

    assert stored(pair.a_client, call["id"]).state == "ringing"


def test_rejoin_while_active(calls, pair, sockets):
    call = answered(pair)
    answered_at = stored(pair.a_client, call["id"]).answered_at
    for ws in (sockets.a, sockets.b):
        frames(ws)

    for client in (pair.a_client, pair.b_client, pair.b_client):
        assert take_token(client, call["id"]).status_code == 200

    assert stored(pair.a_client, call["id"]).answered_at == answered_at
    assert frames(sockets.a) == frames(sockets.b) == []


def test_token_leaves_voice_channels_and_other_call_rooms(
        client, calls, rooms, pair, new_client, monkeypatch):
    carol_client = new_client()
    register(carol_client, "carol-leave")
    other = open_conversation(carol_client, pair.b["id"])
    earlier = started(carol_client, other["id"])
    call = started(pair.a_client, pair.convo)
    source = _presence_with(client, monkeypatch, {77: _in_voice(pair.b["id"])})

    assert take_token(pair.b_client, call["id"]).status_code == 200

    assert source.removed == [(77, pair.b["id"])]
    assert rooms.removed == [(earlier["id"], pair.b["id"])]


def test_channel_token_leaves_call_rooms(calls, rooms, pair):
    call = started(pair.a_client, pair.convo)
    server = create_server(pair.a_client)
    channel = create_channel(pair.a_client, server["id"], "Hangout", "voice")

    res = pair.a_client.post("/api/voice/token", json={"channel_id": channel["id"]})

    assert res.status_code == 200, res.text
    assert rooms.removed == [(call["id"], pair.a["id"])]


# -- decline -----------------------------------------------------------------

def test_decline_by_callee(calls, rooms, pair, sockets):
    call = started(pair.a_client, pair.convo)
    for ws in (sockets.a, sockets.b):
        frames(ws)

    res = decline(pair.b_client, call["id"])

    assert res.status_code == 200
    assert (res.json()["state"], res.json()["reason"]) == ("declined", "declined")
    calls.drain()
    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason, row.open_key) == ("declined", "declined", None)
    assert row.ended_at is not None
    assert rooms.deleted == [call["id"]]
    message = stored_message(pair.a_client, call)
    assert text_of(message.content) == "Call declined"
    assert message.edited_at is not None
    for ws in (sockets.a, sockets.b):
        (frame,) = frames(ws)
        assert frame["call"]["state"] == "declined"
        assert frame["content"] == text_doc("Call declined")
    assert active_ids(pair.a_client) == active_ids(pair.b_client) == []


def test_decline_by_the_caller_is_403(calls, pair):
    call = started(pair.a_client, pair.convo)

    assert decline(pair.a_client, call["id"]).status_code == 403
    assert stored(pair.a_client, call["id"]).state == "ringing"


@pytest.mark.parametrize("how, state", [
    ("declined", "declined"), ("missed", "missed"), ("ended", "ended")])
def test_decline_of_a_finished_call_changes_nothing(calls, clock, pair, sockets, how, state):
    if how == "declined":
        call = started(pair.a_client, pair.convo)
        assert decline(pair.b_client, call["id"]).status_code == 200
    elif how == "missed":
        call = timed_out(pair, clock, calls)
    else:
        call = ended(pair, calls)
    for ws in (sockets.a, sockets.b):
        frames(ws)

    for client in (pair.b_client, pair.a_client):
        res = decline(client, call["id"])
        assert res.status_code == 200 and res.json()["state"] == state
    assert frames(sockets.a) == frames(sockets.b) == []


def test_decline_of_an_active_call_is_409(calls, pair):
    call = answered(pair)

    assert decline(pair.b_client, call["id"]).status_code == 409
    assert stored(pair.a_client, call["id"]).state == "active"


def test_decline_by_an_outsider_is_404(calls, pair, new_client):
    call = started(pair.a_client, pair.convo)
    carol = new_client()
    register(carol, "carol-decline")

    assert decline(carol, call["id"]).status_code == 404


# -- active and refresh ------------------------------------------------------

def test_active_applies_ring_expiry(calls, clock, pair):
    call = started(pair.a_client, pair.convo)
    clock.advance(seconds=31)

    assert active_ids(pair.b_client) == []

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason, row.open_key) == ("missed", "timeout", None)


def test_refresh_reads_the_room_now(calls, rooms, clock, pair):
    call = started(pair.a_client, pair.convo)
    url = f"/api/calls/{call['id']}/refresh"
    rooms.occupants[call["id"]] = {pair.a["id"]}
    assert pair.a_client.post(url).status_code == 204
    calls.drain()
    rooms.occupants[call["id"]] = set()
    assert pair.a_client.post(url).status_code == 204
    calls.drain()
    clock.advance(seconds=10)

    assert pair.a_client.post(url).status_code == 204
    calls.drain()

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason) == ("missed", "cancelled")


def test_refresh_of_a_finished_call_reads_nothing(calls, rooms, pair):
    call = started(pair.a_client, pair.convo)
    assert decline(pair.b_client, call["id"]).status_code == 200
    calls.drain()
    rooms.reads.clear()

    assert pair.a_client.post(f"/api/calls/{call['id']}/refresh").status_code == 204
    calls.drain()

    assert rooms.reads == []


def test_refresh_404(calls, pair, new_client):
    call = started(pair.a_client, pair.convo)
    carol = new_client()
    register(carol, "carol-refresh")

    assert carol.post(f"/api/calls/{call['id']}/refresh").status_code == 404


# -- logout ------------------------------------------------------------------

@pytest.mark.parametrize("state, quitter, expected", [
    ("active", "caller", ("ended", "hangup")),
    ("ringing", "caller", ("missed", "cancelled")),
    ("ringing", "callee", ("ringing", None)),
])
def test_logout_ends_what_it_should(calls, pair, state, quitter, expected):
    call = answered(pair) if state == "active" else started(pair.a_client, pair.convo)

    assert pair.client_of[quitter].post("/auth/logout").status_code == 200

    row = stored(pair.a_client, call["id"])
    assert (row.state, row.reason) == expected


def test_logout_with_another_session_keeps_the_call(calls, pair, new_client):
    call = answered(pair)
    second = new_client()
    assert second.post("/auth/login", json={
        "username": pair.a["username"], "password": PASSWORD}).status_code == 200

    with second.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        assert pair.a_client.post("/auth/logout").status_code == 200

        assert stored(pair.a_client, call["id"]).state == "active"


# -- the wire ----------------------------------------------------------------

def test_history_and_by_ids_carry_call_only_on_call_rows(client, calls, pair):
    call = started(pair.a_client, pair.convo)
    plain = uuid.uuid4()
    run(client, Message.create(
        uuid=plain, content=json.dumps(text_doc("hi")), author_id=pair.a["id"],
        conversation_id=pair.convo, timestamp=datetime.now(timezone.utc)))
    expected = stored_message(pair.a_client, call).metadata["call"]

    history = pair.b_client.get(f"/conversations/{pair.convo}/messages").json()["messages"]
    by_ids = pair.b_client.get(
        "/messages", params={"ids": [call["message_id"], str(plain)]}).json()["messages"]

    for rows in (history, by_ids):
        calls_by_id = {row["id"]: row.get("call") for row in rows}
        assert calls_by_id == {call["message_id"]: expected, str(plain): None}
        assert all(("call" in row) == (row["id"] == call["message_id"]) for row in rows)


@pytest.mark.parametrize("make, text", [
    (lambda pair, clock, calls: started(pair.a_client, pair.convo), "Started a call"),
    (lambda pair, clock, calls: answered(pair), "Call in progress"),
    (lambda pair, clock, calls: decline_it(pair, clock), "Call declined"),
    (lambda pair, clock, calls: timed_out(pair, clock, calls), "Missed call"),
    (lambda pair, clock, calls: ended(pair, calls), "Call ended · 0:00"),
], ids=["ringing", "active", "declined", "missed", "ended"])
def test_preview_shows_the_fallback_text(calls, clock, pair, make, text):
    make(pair, clock, calls)

    preview = conversation_row(pair.b_client, pair.convo)["last_message"]

    assert text_of(preview["content"]) == text


@pytest.mark.parametrize("seconds, expected", [
    (5, "Call ended · 0:05"), (252, "Call ended · 4:12"), (3723, "Call ended · 1:02:03"),
    (None, "Call ended"),
])
def test_fallback_text_of_an_ended_call(seconds, expected):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    call = SimpleNamespace(
        state=CallState.ENDED, answered_at=None if seconds is None else start,
        ended_at=start + timedelta(seconds=seconds or 0))

    assert fallback_text(call) == expected


@pytest.mark.parametrize("key", ["call", "embeds", "imported_author", "import"])
def test_client_metadata_keys_are_stripped(client, pair, key):
    frame = dm_frame(pair.convo, metadata={key: {"state": "forged"}})
    with pair.a_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(frame)
        while ws.receive_json()["type"] != "message_ack":
            pass

    stored_row = run(client, Message.get(uuid=frame["id"]))
    assert key not in stored_row.metadata
    history = pair.b_client.get(f"/conversations/{pair.convo}/messages").json()["messages"]
    assert "call" not in history[0]


def test_changes_feed_lists_an_ended_call_row(calls, pair):
    call = answered(pair)
    cursor = cursor_of(pair.b_client)

    calls.end_active(pair.a["id"])

    assert changes_since(pair.b_client, cursor) == {(call["message_id"], "edited")}


# -- push --------------------------------------------------------------------

def ring_pushes(recorder):
    return [c for c in recorder.calls if c["payload"]["kind"] == "dm"]


def close_pushes(recorder):
    return [c for c in recorder.calls if c["payload"]["kind"] == "read"]


@pytest.fixture
def subscribed(pair, push_on):  # noqa: F811
    """bob with a Chrome and an Apple subscription."""
    assert subscribe(pair.b_client)[0].status_code == 201
    assert subscribe(pair.b_client, f"{APPLE}{uuid.uuid4().hex}")[0].status_code == 201
    return push_on


def test_ring_push_when_the_callee_is_inactive(calls, pair, subscribed):
    call = started(pair.a_client, pair.convo)
    drain_push(pair.a_client)

    pushes = ring_pushes(subscribed)
    assert len(pushes) == 2
    for push_call in pushes:
        assert push_call["user_id"] == pair.b["id"]
        assert (push_call["topic"], push_call["urgency"], push_call["ttl"]) == (
            f"call-{call['id']}", "high", 60)
        payload = push_call["payload"]
        assert payload["tag"] == f"call-{call['id']}" and payload["title"] == pair.a["username"]
        assert payload["body"] == "Incoming call"
        assert payload["url"] == f"/app/direct/{pair.convo}/"
        assert payload["message_uuid"] == call["message_id"]


def test_no_ring_push_when_the_callee_is_active(calls, pair, subscribed):
    with pair.b_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        report(ws, "active")
        started(pair.a_client, pair.convo)
        drain_push(pair.a_client)

    assert subscribed.calls == []


@pytest.mark.parametrize("answer", [
    lambda pair, call: take_token(pair.b_client, call["id"]),
    lambda pair, call: start_call(pair.b_client, pair.convo),
    lambda pair, call: decline(pair.b_client, call["id"]),
], ids=["token", "crossing", "decline"])
def test_ring_push_is_closed_once(calls, pair, subscribed, answer):
    call = started(pair.a_client, pair.convo)
    drain_push(pair.a_client)

    assert answer(pair, call).status_code == 200
    calls.end_active(pair.a["id"])
    calls.end_active(pair.b["id"])
    drain_push(pair.a_client)

    (close,) = close_pushes(subscribed)
    assert close["payload"]["tag"] == f"call-{call['id']}"
    assert (close["topic"], close["urgency"]) == (f"call-{call['id']}", "normal")
    assert not close["endpoint"].startswith(APPLE)


@pytest.mark.parametrize("how", ["timeout", "cancelled"])
def test_missed_sends_nothing(calls, rooms, clock, pair, subscribed, how):
    call = started(pair.a_client, pair.convo)
    drain_push(pair.a_client)
    if how == "timeout":
        clock.advance(seconds=31)
    else:
        rooms.occupants[call["id"]] = {pair.a["id"]}
        calls.tick()
        rooms.occupants[call["id"]] = set()
        calls.tick()
        clock.advance(seconds=10)
    calls.tick()
    drain_push(pair.a_client)

    assert stored(pair.a_client, call["id"]).state == "missed"
    assert close_pushes(subscribed) == []
    assert len(ring_pushes(subscribed)) == 2
