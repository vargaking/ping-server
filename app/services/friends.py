"""Friend requests and friendships between two users.

Each transition is one statement, arbitrated by the unique pair or by a filter on
the expected state, so requests racing for the same pair settle on one outcome
without a transaction. ``announce`` tells both users' sockets and, for a new
request, the target's browsers.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from app.models.Friendship import Friendship, FriendshipStatus
from app.models.User import User
from app.routers.users import UserResponse
from app.services.push import PAYLOAD_VERSION, push

MAX_ATTEMPTS = 3
FRIENDS_URL = "/app/direct/friends/"


class Reason(StrEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    CROSSED = "crossed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    REMOVED = "removed"


class FriendState(StrEnum):
    NONE = "none"
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    FRIENDS = "friends"


class FriendshipConflict(Exception):
    """The pair kept changing under a request; the caller can simply try again."""


@dataclass(frozen=True)
class Change:
    """What a call did. ``reason`` is None when nothing changed. ``row`` is None
    once the pair has no row. ``at`` is when the resulting state began."""
    actor_id: int
    other_id: int
    row: Friendship | None
    reason: Reason | None
    at: datetime


def state_for(row: Friendship | None, viewer_id: int) -> FriendState:
    """The state of *row* as *viewer_id* sees it."""
    if row is None:
        return FriendState.NONE
    if row.status == FriendshipStatus.ACCEPTED:
        return FriendState.FRIENDS
    if row.requester_id == viewer_id:
        return FriendState.OUTGOING
    return FriendState.INCOMING


async def find(user_a_id: int, user_b_id: int) -> Friendship | None:
    """The row for a normalized pair."""
    return await Friendship.get_or_none(user_a_id=user_a_id, user_b_id=user_b_id)


async def _counterpart_ids(user_id: int, status: FriendshipStatus) -> set[int]:
    pairs = await Friendship.filter(
        Q(user_a_id=user_id) | Q(user_b_id=user_id), status=status
    ).values_list("user_a_id", "user_b_id")
    return {uid for pair in pairs for uid in pair} - {user_id}


async def friend_ids(user_id: int) -> set[int]:
    """The IDs of *user_id*'s friends."""
    return await _counterpart_ids(user_id, FriendshipStatus.ACCEPTED)


async def pending_counterpart_ids(user_id: int) -> set[int]:
    """The IDs of users with a pending request to or from *user_id*."""
    return await _counterpart_ids(user_id, FriendshipStatus.PENDING)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def send_request(me_id: int, other_id: int) -> Change:
    """Ask *other_id* to be friends, or accept if they already asked me."""
    a, b = Friendship.normalize_pair(me_id, other_id)
    for _ in range(MAX_ATTEMPTS):
        row = await find(a, b)
        if row is None:
            try:
                row = await Friendship.create(
                    user_a_id=a, user_b_id=b, requester_id=me_id,
                    status=FriendshipStatus.PENDING)
            except IntegrityError:
                continue  # the other side's insert won; read it next round
            return Change(me_id, other_id, row, Reason.REQUESTED, row.created_at)
        if row.status == FriendshipStatus.ACCEPTED:
            return Change(me_id, other_id, row, None, row.accepted_at)
        if row.requester_id == me_id:
            return Change(me_id, other_id, row, None, row.created_at)
        now = _now()
        accepted = await Friendship.filter(
            id=row.id, status=FriendshipStatus.PENDING
        ).update(status=FriendshipStatus.ACCEPTED, accepted_at=now)
        if accepted:
            row.status, row.accepted_at = FriendshipStatus.ACCEPTED, now
            return Change(me_id, other_id, row, Reason.CROSSED, now)
    raise FriendshipConflict


async def accept(me_id: int, other_id: int) -> Change | None:
    """Accept *other_id*'s request. None when there is nothing to accept."""
    a, b = Friendship.normalize_pair(me_id, other_id)
    now = _now()
    accepted = await Friendship.filter(
        user_a_id=a, user_b_id=b, requester_id=other_id, status=FriendshipStatus.PENDING
    ).update(status=FriendshipStatus.ACCEPTED, accepted_at=now)
    row = await find(a, b)
    if row is None or row.status != FriendshipStatus.ACCEPTED:
        return None
    if accepted:
        return Change(me_id, other_id, row, Reason.ACCEPTED, row.accepted_at)
    return Change(me_id, other_id, row, None, row.accepted_at)


async def _delete(
    me_id: int, other_id: int, reason: Reason, **expected,
) -> Change:
    a, b = Friendship.normalize_pair(me_id, other_id)
    # Taken before the delete so a request made right after it is always newer.
    at = _now()
    deleted = await Friendship.filter(user_a_id=a, user_b_id=b, **expected).delete()
    return Change(me_id, other_id, None, reason if deleted else None, at)


async def decline(me_id: int, other_id: int) -> Change:
    """Drop *other_id*'s pending request to me."""
    return await _delete(
        me_id, other_id, Reason.DECLINED,
        requester_id=other_id, status=FriendshipStatus.PENDING)


async def cancel(me_id: int, other_id: int) -> Change:
    """Withdraw my pending request to *other_id*."""
    return await _delete(
        me_id, other_id, Reason.CANCELLED,
        requester_id=me_id, status=FriendshipStatus.PENDING)


async def remove(me_id: int, other_id: int) -> Change:
    """End an accepted friendship."""
    return await _delete(
        me_id, other_id, Reason.REMOVED, status=FriendshipStatus.ACCEPTED)


async def list_for(user_id: int) -> tuple[list[Friendship], dict[int, User]]:
    """Every row *user_id* is part of, and the other users in them by id."""
    rows = await Friendship.filter(Q(user_a_id=user_id) | Q(user_b_id=user_id))
    others = {row.other_user_id(user_id) for row in rows}
    users = await User.filter(id__in=others) if others else []
    return rows, {user.id: user for user in users}


def _frame(change: Change, viewer_id: int, subject: User) -> dict:
    return {
        "type": "friendship_updated",
        "user_id": subject.id,
        "state": state_for(change.row, viewer_id).value,
        "reason": change.reason.value,
        "actor_id": change.actor_id,
        "user": UserResponse.from_user(subject).model_dump(mode="json"),
        "created_at": change.at.isoformat(),
    }


def request_payload(requester: User) -> dict:
    return {
        "v": PAYLOAD_VERSION,
        "kind": "friend_request",
        "tag": f"friend-request-{requester.id}",
        "title": requester.username,
        "body": "sent you a friend request",
        "url": FRIENDS_URL,
        "sent_at": _now().isoformat(),
    }


async def _retract_presence(comms, user_a_id: int, user_b_id: int) -> None:
    """Tell two ex-friends the other is offline, unless something else still
    puts them in each other's presence."""
    if user_b_id in await comms.get_presence_audience_ids(user_a_id):
        return
    for viewer_id, subject_id in ((user_a_id, user_b_id), (user_b_id, user_a_id)):
        if comms.connection_manager.is_online(viewer_id):
            await comms.send_to_user(viewer_id, {
                "type": "presence_update", "user_id": subject_id, "online": False})


async def announce(comms, change: Change, me: User, other: User) -> None:
    """Send both users the new state, move their presence if it changed, and
    push a new request to *other* when they have no active session."""
    if comms is None or change.reason is None:
        return
    for viewer, subject in ((me, other), (other, me)):
        await comms.send_to_user(viewer.id, _frame(change, viewer.id, subject))

    if change.reason in (Reason.ACCEPTED, Reason.CROSSED):
        await comms.announce_presence_between(me.id, other.id)
    elif change.reason == Reason.REMOVED:
        await _retract_presence(comms, me.id, other.id)
    elif (change.reason == Reason.REQUESTED and push.enabled
            and not comms.connection_manager.is_active(other.id)):
        payload = request_payload(me)
        push.schedule(push.send_to_user(
            other.id, payload, topic=payload["tag"], urgency="normal"))
