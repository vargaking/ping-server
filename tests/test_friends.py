"""Friends: requests and the friends list.

A pending row is a request, an accepted row a friendship. Both users hear every
change on all their sockets as a ``friendship_updated`` frame written from their
own side. Friends see each other's presence and profile changes without sharing
a server; a pending request shares profile changes only.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import NamedTuple

import pytest
from starlette.testclient import TestClient

from app.models.Friendship import Friendship
from app.models.User import User
from app.rate_limit import limiter
from app.services import friends as friends_service
from app.services.system_user import IMPORTED_PASSWORD_HASH, IMPORTED_USERNAME
from tests.conftest import ORIGIN, create_server, register, ws_ready
from tests.test_push import push_on, report, subscribe, vapid_env  # noqa: F401
from tests.test_push import drain as drain_push
from tests.test_push import run
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}
NOBODY = 99999


class Account(NamedTuple):
    client: TestClient
    me: dict

    @property
    def id(self) -> int:
        return self.me["id"]

    @property
    def name(self) -> str:
        return self.me["username"]


@pytest.fixture
def make_account(client, new_client):
    """Registers accounts with no server and no conversation between them. The
    first one uses the primary client, so ids rise in the order of the calls."""
    made = []

    def make(username: str) -> Account:
        account_client = new_client() if made else client
        account = Account(account_client, register(account_client, username))
        made.append(account)
        return account

    return make


@pytest.fixture
def duo(make_account):
    """ada and bob; ada has the lower id."""
    return make_account("ada"), make_account("bob")


@pytest.fixture
def fresh_limits():
    limiter.reset()
    yield
    limiter.reset()


ASKER_ORDER = pytest.mark.parametrize(
    "asker_low", [True, False], ids=["asker-has-lower-id", "asker-has-higher-id"])


def ordered(duo, asker_low):
    """(asker, target) with the asker holding the lower or the higher id."""
    return duo if asker_low else duo[::-1]


def send(account, **target):
    return account.client.post("/friends/requests", json=target)


def accept(account, other):
    return account.client.post(f"/friends/{other.id}/accept")


def decline(account, other):
    return account.client.post(f"/friends/{other.id}/decline")


def cancel(account, other):
    return account.client.delete(f"/friends/{other.id}/request")


def remove(account, other):
    return account.client.delete(f"/friends/{other.id}")


def befriend(asker, target):
    assert send(asker, user_id=target.id).status_code == 200
    assert accept(target, asker).status_code == 200


def lists(account) -> dict:
    res = account.client.get("/friends/")
    assert res.status_code == 200, res.text
    return res.json()


def ids(entries) -> list[int]:
    return [entry["user"]["id"] for entry in entries]


def rows(account) -> list[tuple[int, int, str]]:
    async def read():
        return await Friendship.all().order_by("id").values_list(
            "user_a_id", "user_b_id", "status")

    return run(account.client, read())


def same_instant(first: str, second: str) -> bool:
    return datetime.fromisoformat(first) == datetime.fromisoformat(second)


@contextmanager
def sockets(*accounts):
    """One connected socket per account, listed in order."""
    with ExitStack() as stack:
        opened = []
        for account in accounts:
            ws = stack.enter_context(account.client.websocket_connect("/ws", headers=HEADERS))
            ws_ready(ws)
            opened.append(ws)
        yield opened


def received(ws, *types: str) -> list[dict]:
    """Everything the server has sent on *ws* so far, optionally only of *types*.
    Frames arrive in order, so the pong closes the list."""
    ws.send_json({"type": "ping", "t": 1.0})
    seen = []
    while (frame := ws.receive_json())["type"] != "pong":
        seen.append(frame)
    return [f for f in seen if f["type"] in types] if types else seen


def friendship_frames(ws) -> list[dict]:
    return received(ws, "friendship_updated")


def brief(frame: dict) -> tuple:
    return frame["user_id"], frame["state"], frame["reason"], frame["actor_id"]


# --- sending ---------------------------------------------------------------

@pytest.mark.parametrize("by", ["user_id", "username"])
@ASKER_ORDER
def test_send_lists_the_request_on_both_sides(duo, by, asker_low):
    asker, target = ordered(duo, asker_low)
    body = {"user_id": target.id} if by == "user_id" else {"username": target.name}

    with sockets(asker, target) as (asker_ws, target_ws):
        res = send(asker, **body)
        to_asker, to_target = friendship_frames(asker_ws), friendship_frames(target_ws)

    assert res.status_code == 200, res.text
    result = res.json()
    assert result["state"] == "outgoing"
    assert result["user"]["id"] == target.id
    assert result["user"]["username"] == target.name

    mine, theirs = lists(asker), lists(target)
    assert (ids(mine["outgoing"]), mine["incoming"], mine["friends"]) == ([target.id], [], [])
    assert (ids(theirs["incoming"]), theirs["outgoing"], theirs["friends"]) == ([asker.id], [], [])
    assert mine["outgoing"][0]["created_at"] == result["created_at"]

    [sent], [got] = to_asker, to_target
    assert brief(sent) == (target.id, "outgoing", "requested", asker.id)
    assert brief(got) == (asker.id, "incoming", "requested", asker.id)
    assert sent["user"]["username"] == target.name
    assert got["user"]["username"] == asker.name
    assert same_instant(sent["created_at"], result["created_at"])
    assert same_instant(got["created_at"], result["created_at"])


def test_repeating_a_request_changes_nothing(duo):
    asker, target = duo
    first = send(asker, user_id=target.id).json()

    with sockets(asker, target) as (asker_ws, target_ws):
        again = send(asker, username=target.name)
        assert friendship_frames(asker_ws) == []
        assert friendship_frames(target_ws) == []

    assert again.status_code == 200
    assert again.json() == first
    assert len(rows(asker)) == 1


def test_send_to_a_friend_changes_nothing(duo):
    asker, target = duo
    befriend(asker, target)
    since = lists(asker)["friends"][0]["since"]

    with sockets(asker, target) as (asker_ws, target_ws):
        res = send(target, user_id=asker.id)
        assert friendship_frames(asker_ws) == []
        assert friendship_frames(target_ws) == []

    body = res.json()
    assert (res.status_code, body["state"]) == (200, "friends")
    assert same_instant(body["created_at"], since)
    assert len(rows(asker)) == 1


def imported_account(client) -> int:
    async def create():
        user = await User.create(
            username=IMPORTED_USERNAME, password_hash=IMPORTED_PASSWORD_HASH, profile={})
        return user.id

    return run(client, create())


REJECTED_SENDS = [
    pytest.param(lambda me, other, imported: {}, 422, id="neither-target"),
    pytest.param(
        lambda me, other, imported: {"user_id": other.id, "username": other.name},
        422, id="both-targets"),
    pytest.param(
        lambda me, other, imported: {"username": ""}, 422, id="empty-username"),
    pytest.param(
        lambda me, other, imported: {"username": other.name.upper()}, 404, id="wrong-case"),
    pytest.param(
        lambda me, other, imported: {"user_id": NOBODY}, 404, id="unknown-id"),
    pytest.param(
        lambda me, other, imported: {"username": "nobody-here"}, 404, id="unknown-username"),
    pytest.param(
        lambda me, other, imported: {"user_id": imported}, 404, id="imported-by-id"),
    pytest.param(
        lambda me, other, imported: {"username": IMPORTED_USERNAME}, 404,
        id="imported-by-username"),
    pytest.param(lambda me, other, imported: {"user_id": me.id}, 400, id="self-by-id"),
    pytest.param(lambda me, other, imported: {"username": me.name}, 400, id="self-by-username"),
]


@pytest.mark.parametrize("build, expected", REJECTED_SENDS)
def test_send_rejects(duo, build, expected):
    me, other = duo
    res = send(me, **build(me, other, imported_account(me.client)))
    assert res.status_code == expected, res.text
    assert rows(me) == []


def test_sending_to_someone_who_asked_first_makes_you_friends(duo):
    ada, bob = duo
    asked = send(ada, user_id=bob.id).json()

    with sockets(ada, bob) as (ada_ws, bob_ws):
        res = send(bob, user_id=ada.id)
        to_ada, to_bob = friendship_frames(ada_ws), friendship_frames(bob_ws)

    body = res.json()
    assert (res.status_code, body["state"], body["user"]["id"]) == (200, "friends", ada.id)
    assert rows(ada) == [(ada.id, bob.id, "accepted")]
    [for_ada], [for_bob] = to_ada, to_bob
    assert brief(for_ada) == (bob.id, "friends", "crossed", bob.id)
    assert brief(for_bob) == (ada.id, "friends", "crossed", bob.id)
    assert same_instant(for_ada["created_at"], body["created_at"])
    assert not same_instant(body["created_at"], asked["created_at"])
    for account, other in ((ada, bob), (bob, ada)):
        mine = lists(account)
        assert (ids(mine["friends"]), mine["incoming"], mine["outgoing"]) == ([other.id], [], [])


def test_requests_crossing_end_as_one_accepted_row(duo, monkeypatch):
    ada, bob = duo
    send(ada, user_id=bob.id)
    real_find = friends_service.find
    calls = []

    async def blind_first(user_a_id, user_b_id):
        calls.append((user_a_id, user_b_id))
        return None if len(calls) == 1 else await real_find(user_a_id, user_b_id)

    monkeypatch.setattr(friends_service, "find", blind_first)

    res = send(bob, user_id=ada.id)

    assert (res.status_code, res.json()["state"]) == (200, "friends")
    assert len(calls) == 2
    assert rows(ada) == [(ada.id, bob.id, "accepted")]


def test_send_answers_409_when_the_pair_will_not_settle(duo, monkeypatch):
    ada, bob = duo
    send(ada, user_id=bob.id)

    async def never_found(user_a_id, user_b_id):
        return None

    monkeypatch.setattr(friends_service, "find", never_found)

    res = send(bob, user_id=ada.id)

    assert (res.status_code, res.json()["detail"]) == (409, "Try again")
    assert rows(ada) == [(ada.id, bob.id, "pending")]


# --- accepting -------------------------------------------------------------

def test_accept_makes_friends_and_tells_every_socket(duo):
    asker, target = duo
    send(asker, user_id=target.id)

    with sockets(asker, target, target) as (asker_ws, first_tab, second_tab):
        res = accept(target, asker)
        to_asker = friendship_frames(asker_ws)
        to_target = friendship_frames(first_tab), friendship_frames(second_tab)

        body = res.json()
        assert (res.status_code, body["state"], body["user"]["id"]) == (200, "friends", asker.id)
        [for_asker] = to_asker
        assert brief(for_asker) == (target.id, "friends", "accepted", target.id)
        assert same_instant(for_asker["created_at"], body["created_at"])
        for [frame] in to_target:
            assert brief(frame) == (asker.id, "friends", "accepted", target.id)
            assert same_instant(frame["created_at"], body["created_at"])

        again = accept(target, asker)
        assert (again.status_code, again.json()) == (200, body)
        assert friendship_frames(asker_ws) == []
        assert friendship_frames(first_tab) == []
        assert friendship_frames(second_tab) == []

    assert same_instant(lists(asker)["friends"][0]["since"], body["created_at"])
    assert same_instant(lists(target)["friends"][0]["since"], body["created_at"])
    assert lists(asker)["outgoing"] == [] and lists(target)["incoming"] == []


@pytest.mark.parametrize("setup, target_id", [
    pytest.param(lambda me, other: None, "other", id="no-request"),
    pytest.param(lambda me, other: send(me, user_id=other.id), "other", id="my-own-request"),
    pytest.param(lambda me, other: None, NOBODY, id="unknown-user"),
])
def test_accept_without_an_incoming_request_is_404(duo, setup, target_id):
    me, other = duo
    setup(me, other)
    before = rows(me)

    res = me.client.post(f"/friends/{other.id if target_id == 'other' else target_id}/accept")

    assert (res.status_code, res.json()["detail"]) == (404, "No friend request from this user")
    assert rows(me) == before


# --- declining, cancelling, removing ---------------------------------------

def test_decline_deletes_the_request_and_the_sender_can_ask_again(duo):
    asker, target = duo
    first = send(asker, user_id=target.id).json()

    with sockets(asker, target) as (asker_ws, target_ws):
        res = decline(target, asker)
        to_asker, to_target = friendship_frames(asker_ws), friendship_frames(target_ws)

    assert res.status_code == 204
    [for_asker], [for_target] = to_asker, to_target
    assert brief(for_asker) == (target.id, "none", "declined", target.id)
    assert brief(for_target) == (asker.id, "none", "declined", target.id)
    assert rows(asker) == []
    assert lists(asker) == {"friends": [], "incoming": [], "outgoing": []}
    assert lists(target) == {"friends": [], "incoming": [], "outgoing": []}

    again = send(asker, user_id=target.id)
    assert (again.status_code, again.json()["state"]) == (200, "outgoing")
    assert datetime.fromisoformat(again.json()["created_at"]) > datetime.fromisoformat(
        first["created_at"])


@pytest.mark.parametrize("act, befriended, actor, reason", [
    pytest.param(cancel, False, "asker", "cancelled", id="cancel"),
    pytest.param(remove, True, "asker", "removed", id="remove-by-asker"),
    pytest.param(remove, True, "target", "removed", id="remove-by-target"),
])
def test_cancel_and_remove_clear_both_lists(duo, act, befriended, actor, reason):
    asker, target = duo
    send(asker, user_id=target.id)
    if befriended:
        accept(target, asker)
    me, other = (asker, target) if actor == "asker" else (target, asker)

    with sockets(asker, target) as (asker_ws, target_ws):
        received(asker_ws), received(target_ws)
        res = act(me, other)
        frames = {asker.id: friendship_frames(asker_ws), target.id: friendship_frames(target_ws)}

    assert res.status_code == 204
    for viewer, subject in ((asker, target), (target, asker)):
        [frame] = frames[viewer.id]
        assert brief(frame) == (subject.id, "none", reason, me.id)
        assert frame["user"]["id"] == subject.id
        assert lists(viewer) == {"friends": [], "incoming": [], "outgoing": []}
    assert rows(asker) == []


def test_remove_keeps_the_conversation(duo):
    ada, bob = duo
    befriend(ada, bob)
    assert ada.client.post("/conversations/", json={"user_id": bob.id}).status_code == 200

    assert remove(ada, bob).status_code == 204

    for account, other in ((ada, bob), (bob, ada)):
        listed = account.client.get("/conversations/").json()
        assert [c["other_user"]["id"] for c in listed] == [other.id]


NOTHING_TO_DO = [
    pytest.param(decline, "none", "target", id="decline-no-row"),
    pytest.param(decline, "pending", "asker", id="decline-my-own-request"),
    pytest.param(decline, "friends", "target", id="decline-a-friend"),
    pytest.param(cancel, "none", "asker", id="cancel-no-row"),
    pytest.param(cancel, "pending", "target", id="cancel-their-request"),
    pytest.param(cancel, "friends", "asker", id="cancel-a-friend"),
    pytest.param(remove, "none", "asker", id="remove-no-row"),
    pytest.param(remove, "pending", "asker", id="remove-my-pending-request"),
    pytest.param(remove, "pending", "target", id="remove-their-pending-request"),
]


@pytest.mark.parametrize("act, state, actor", NOTHING_TO_DO)
def test_acting_on_the_wrong_state_is_a_silent_no_op(duo, act, state, actor):
    asker, target = duo
    if state != "none":
        send(asker, user_id=target.id)
    if state == "friends":
        accept(target, asker)
    me, other = (asker, target) if actor == "asker" else (target, asker)
    before = rows(asker), lists(asker), lists(target)

    with sockets(asker, target) as (asker_ws, target_ws):
        received(asker_ws), received(target_ws)
        res = act(me, other)
        assert friendship_frames(asker_ws) == []
        assert friendship_frames(target_ws) == []

    assert res.status_code == 204
    assert (rows(asker), lists(asker), lists(target)) == before


@pytest.mark.parametrize("act", [decline, cancel, remove])
def test_acting_on_an_unknown_user_is_a_silent_no_op(duo, act):
    me, _ = duo
    res = act(me, Account(me.client, {"id": NOBODY, "username": "nobody"}))
    assert res.status_code == 204


# --- the list --------------------------------------------------------------

def test_list_only_shows_my_rows(make_account):
    ada, bob, cy, dan = (make_account(name) for name in ("ada", "bob", "cyd", "dan"))
    send(ada, user_id=bob.id)
    befriend(bob, cy)
    send(dan, user_id=cy.id)

    assert {k: ids(v) for k, v in lists(ada).items()} == {
        "friends": [], "incoming": [], "outgoing": [bob.id]}
    assert {k: ids(v) for k, v in lists(bob).items()} == {
        "friends": [cy.id], "incoming": [ada.id], "outgoing": []}
    assert {k: ids(v) for k, v in lists(cy).items()} == {
        "friends": [bob.id], "incoming": [dan.id], "outgoing": []}
    assert {k: ids(v) for k, v in lists(dan).items()} == {
        "friends": [], "incoming": [], "outgoing": [cy.id]}


def test_requests_are_listed_newest_first(make_account):
    me, first, second, third, fourth = (
        make_account(n) for n in ("meg", "first", "second", "third", "fourth"))
    send(me, user_id=first.id)
    send(me, user_id=second.id)
    send(third, user_id=me.id)
    send(fourth, user_id=me.id)

    mine = lists(me)
    assert ids(mine["outgoing"]) == [second.id, first.id]
    assert ids(mine["incoming"]) == [fourth.id, third.id]


def test_friends_are_listed_by_when_they_became_friends_newest_first(make_account):
    me, first, second = (make_account(n) for n in ("meg", "first", "second"))
    send(me, user_id=first.id)
    send(me, user_id=second.id)
    accept(second, me)
    accept(first, me)

    assert ids(lists(me)["friends"]) == [first.id, second.id]


@pytest.mark.parametrize("method, path, body", [
    pytest.param("GET", "/friends/", None, id="list"),
    pytest.param("POST", "/friends/requests", {"user_id": 1}, id="send"),
    pytest.param("POST", "/friends/1/accept", None, id="accept"),
    pytest.param("POST", "/friends/1/decline", None, id="decline"),
    pytest.param("DELETE", "/friends/1/request", None, id="cancel"),
    pytest.param("DELETE", "/friends/1", None, id="remove"),
])
def test_every_endpoint_needs_a_session(new_client, method, path, body):
    res = new_client().request(method, path, json=body)
    assert res.status_code == 401


# --- limits and cleanup ----------------------------------------------------

def test_request_rate_limit_is_per_user(make_account, fresh_limits, monkeypatch):
    monkeypatch.setenv("FRIEND_REQUEST_RATE_LIMIT", "2/minute")
    ada, bob, cy, dan = (make_account(name) for name in ("ada", "bob", "cyd", "dan"))

    statuses = [send(ada, user_id=other.id).status_code for other in (bob, cy, dan)]

    assert statuses == [200, 200, 429]
    assert send(bob, user_id=cy.id).status_code == 200


def test_deleting_an_account_removes_its_friendships(make_account):
    ada, bob, cy = (make_account(name) for name in ("ada", "bob", "cyd"))
    send(ada, user_id=bob.id)
    befriend(cy, ada)
    befriend(bob, cy)

    assert ada.client.delete(f"/users/{ada.id}").status_code == 204

    assert rows(bob) == [(bob.id, cy.id, "accepted")]
    assert ids(lists(bob)["friends"]) == [cy.id]
    assert lists(bob)["incoming"] == []
    assert lists(cy)["friends"] != []


# --- push ------------------------------------------------------------------

def test_a_request_pushes_when_the_target_has_no_active_session(duo, push_on):  # noqa: F811
    asker, target = duo
    assert subscribe(target.client)[0].status_code in (200, 201)

    send(asker, user_id=target.id)
    send(asker, user_id=target.id)
    drain_push(asker.client)

    [call] = push_on.calls
    assert call["user_id"] == target.id
    assert call["topic"] == f"friend-request-{asker.id}"
    assert call["urgency"] == "normal"
    payload = call["payload"]
    sent_at = payload.pop("sent_at")
    assert datetime.fromisoformat(sent_at)
    assert payload == {
        "v": 1,
        "kind": "friend_request",
        "tag": f"friend-request-{asker.id}",
        "title": asker.name,
        "body": "sent you a friend request",
        "url": "/app/direct/friends/",
    }


def test_a_request_does_not_push_while_the_target_is_active(duo, push_on):  # noqa: F811
    asker, target = duo
    assert subscribe(target.client)[0].status_code in (200, 201)

    with sockets(target) as [target_ws]:
        report(target_ws, "active")
        send(asker, user_id=target.id)
        drain_push(asker.client)

    assert push_on.calls == []


@pytest.mark.parametrize("act", ["accept", "decline", "cancel", "cross", "remove"])
def test_only_a_new_request_pushes(duo, push_on, act):  # noqa: F811
    asker, target = duo
    for account in duo:
        assert subscribe(account.client)[0].status_code in (200, 201)
    send(asker, user_id=target.id)
    if act == "remove":
        accept(target, asker)
    drain_push(asker.client)
    push_on.calls.clear()

    {
        "accept": lambda: accept(target, asker),
        "decline": lambda: decline(target, asker),
        "cancel": lambda: cancel(asker, target),
        "cross": lambda: send(target, user_id=asker.id),
        "remove": lambda: remove(asker, target),
    }[act]()
    drain_push(asker.client)

    assert push_on.calls == []


# --- presence and profile changes -------------------------------------------

@pytest.mark.parametrize("accepted", [True, False], ids=["friends", "pending"])
def test_presence_flows_between_friends_without_a_shared_server(duo, accepted):
    ada, bob = duo
    send(ada, user_id=bob.id)
    if accepted:
        accept(bob, ada)
    online = [{"type": "presence_update", "user_id": bob.id, "online": True}]
    offline = [{"type": "presence_update", "user_id": bob.id, "online": False}]

    with sockets(ada) as [ada_ws]:
        received(ada_ws)
        with bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
            assert ws_ready(bob_ws) == {
                "type": "presence_init", "user_ids": [ada.id] if accepted else []}
            assert received(ada_ws) == (online if accepted else [])
        assert received(ada_ws) == (offline if accepted else [])


@pytest.mark.parametrize("path", ["accept", "cross"])
def test_becoming_friends_announces_presence_both_ways(duo, path):
    ada, bob = duo
    send(ada, user_id=bob.id)

    with sockets(ada, bob) as (ada_ws, bob_ws):
        received(ada_ws), received(bob_ws)
        res = accept(bob, ada) if path == "accept" else send(bob, user_id=ada.id)
        assert res.status_code == 200
        for ws, other in ((ada_ws, bob), (bob_ws, ada)):
            frames = received(ws)
            assert [f["type"] for f in frames] == ["friendship_updated", "presence_update"]
            assert frames[1] == {"type": "presence_update", "user_id": other.id, "online": True}


def share_a_server(ada, bob):
    create_server(ada.client)
    server = ada.client.get("/servers/me").json()[0]
    invite_and_join(ada.client, bob.client, server["id"])


def share_a_conversation(ada, bob):
    assert ada.client.post("/conversations/", json={"user_id": bob.id}).status_code == 200


@pytest.mark.parametrize("shared, retracted", [
    pytest.param(None, True, id="nothing-else-shared"),
    pytest.param(share_a_server, False, id="still-share-a-server"),
    pytest.param(share_a_conversation, False, id="still-have-a-conversation"),
])
def test_removing_a_friend_retracts_presence_unless_something_else_keeps_it(
        duo, shared, retracted):
    ada, bob = duo
    befriend(ada, bob)
    if shared:
        shared(ada, bob)

    with sockets(ada, bob) as (ada_ws, bob_ws):
        received(ada_ws), received(bob_ws)
        assert remove(ada, bob).status_code == 204
        for ws, other in ((ada_ws, bob), (bob_ws, ada)):
            frames = received(ws)
            expected = ["friendship_updated"] + (["presence_update"] if retracted else [])
            assert [f["type"] for f in frames] == expected
            if retracted:
                assert frames[1] == {
                    "type": "presence_update", "user_id": other.id, "online": False}


@pytest.mark.parametrize("relation, reached", [
    pytest.param("friends", True, id="friend"),
    pytest.param("asked-me", True, id="pending-request-to-me"),
    pytest.param("asked-by-me", True, id="pending-request-from-me"),
    pytest.param("stranger", False, id="stranger"),
])
def test_profile_change_reaches_friends_and_pending_requests_only(duo, relation, reached):
    ada, bob = duo
    if relation == "friends":
        befriend(ada, bob)
    elif relation == "asked-me":
        send(bob, user_id=ada.id)
    elif relation == "asked-by-me":
        send(ada, user_id=bob.id)

    with sockets(bob) as [bob_ws]:
        received(bob_ws)
        res = ada.client.put(f"/users/{ada.id}", json={"username": "ada-renamed"})
        assert res.status_code == 200, res.text
        frames = received(bob_ws, "user_invalidate")

    assert frames == ([{"type": "user_invalidate", "user_id": ada.id}] if reached else [])
