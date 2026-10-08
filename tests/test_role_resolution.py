"""The pure role resolver: inheritance inside a role, position across roles."""
from dataclasses import dataclass

from app.permissions import ALL_PERMISSIONS, MEMBER_PERMISSIONS, Permission
from app.services.role_resolution import descendants, member_mask, resolved, would_cycle

SEND = Permission.SEND_MESSAGES
KICK = Permission.KICK_MEMBERS
MANAGE = Permission.MANAGE_MESSAGES


@dataclass
class FakeRole:
    id: int
    position: int = 0
    allow: int = 0
    deny: int = 0
    parent_id: int | None = None
    is_default: bool = False


def roles_of(*roles):
    return {role.id: role for role in roles}


def everyone(allow=MEMBER_PERMISSIONS):
    return FakeRole(id=1, allow=int(allow), is_default=True)


def test_child_allow_beats_parent_deny():
    roles = roles_of(
        FakeRole(id=2, deny=int(KICK)),
        FakeRole(id=3, allow=int(KICK), parent_id=2),
    )
    assert resolved(3, roles) == (int(KICK), 0)


def test_child_deny_beats_parent_allow():
    roles = roles_of(
        FakeRole(id=2, allow=int(KICK | MANAGE)),
        FakeRole(id=3, deny=int(KICK), parent_id=2),
    )
    assert resolved(3, roles) == (int(MANAGE), int(KICK))


def test_grandparent_value_survives_when_nothing_nearer_says_anything():
    roles = roles_of(
        FakeRole(id=2, allow=int(KICK)),
        FakeRole(id=3, parent_id=2),
        FakeRole(id=4, parent_id=3),
    )
    assert resolved(4, roles) == (int(KICK), 0)


def test_muted_above_a_role_that_allows_the_bit_denies_it():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, position=1, allow=int(SEND)),
        FakeRole(id=3, position=2, deny=int(SEND)),
    )
    assert not member_mask(roles, [2, 3]) & SEND


def test_muted_below_a_role_that_says_nothing_about_the_bit_still_denies_it():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, position=2, allow=int(KICK)),
        FakeRole(id=3, position=1, deny=int(SEND)),
    )
    mask = member_mask(roles, [2, 3])
    assert not mask & SEND
    assert mask & KICK


def test_role_above_muted_that_allows_the_bit_wins():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, position=2, allow=int(SEND)),
        FakeRole(id=3, position=1, deny=int(SEND)),
    )
    assert member_mask(roles, [2, 3]) & SEND


def test_unrelated_roles_add_up():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, position=1, allow=int(KICK)),
        FakeRole(id=3, position=2, allow=int(MANAGE)),
    )
    assert member_mask(roles, [2, 3]) == MEMBER_PERMISSIONS | KICK | MANAGE


def test_default_role_deny_is_ignored_and_unassigned_roles_do_not_apply():
    roles = roles_of(
        FakeRole(id=1, allow=int(MEMBER_PERMISSIONS), deny=int(KICK), is_default=True),
        FakeRole(id=2, position=1, allow=int(KICK)),
    )
    assert member_mask(roles, []) == MEMBER_PERMISSIONS
    assert member_mask(roles, [999]) == MEMBER_PERMISSIONS


def test_equal_positions_are_applied_in_id_order():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, allow=int(KICK)),
        FakeRole(id=3, deny=int(KICK)),
    )
    assert not member_mask(roles, [3, 2]) & KICK


def test_mask_is_limited_to_known_bits():
    roles = roles_of(everyone(allow=(1 << 40) | int(SEND)))
    assert member_mask(roles, []) == SEND
    assert member_mask(roles, []) & ~ALL_PERMISSIONS == 0


def test_a_parent_cycle_in_old_data_terminates():
    roles = roles_of(
        everyone(),
        FakeRole(id=2, position=1, allow=int(KICK), parent_id=3),
        FakeRole(id=3, position=2, allow=int(MANAGE), parent_id=2),
    )
    assert resolved(2, roles) == (int(KICK | MANAGE), 0)
    assert member_mask(roles, [2]) == MEMBER_PERMISSIONS | KICK | MANAGE
    assert descendants(2, roles) == {3}
    assert would_cycle(2, 3, roles)


def test_a_bit_in_both_allow_and_deny_of_one_role_counts_as_deny():
    roles = roles_of(everyone(), FakeRole(id=2, position=1, allow=int(KICK | SEND), deny=int(KICK)))
    assert resolved(2, roles) == (int(SEND), int(KICK))
    assert member_mask(roles, [2]) == MEMBER_PERMISSIONS & ~SEND | SEND


def test_would_cycle():
    roles = roles_of(
        FakeRole(id=2),
        FakeRole(id=3, parent_id=2),
        FakeRole(id=4, parent_id=3),
    )
    assert would_cycle(2, 4, roles)
    assert would_cycle(2, 2, roles)
    assert not would_cycle(4, 2, roles)
    assert not would_cycle(3, None, roles)


def test_descendants_are_the_whole_subtree():
    roles = roles_of(
        FakeRole(id=2),
        FakeRole(id=3, parent_id=2),
        FakeRole(id=4, parent_id=3),
        FakeRole(id=5, parent_id=2),
        FakeRole(id=6),
    )
    assert descendants(2, roles) == {3, 4, 5}
    assert descendants(4, roles) == set()
