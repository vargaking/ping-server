from typing import Iterable, Mapping, Protocol

from ..permissions import ALL_PERMISSIONS, Permission

# The bits a channel or category overwrite may change.
CHANNEL_BITS = int(
    Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES | Permission.MANAGE_MESSAGES
    | Permission.CONNECT | Permission.SPEAK | Permission.STREAM)

# (allow, deny) of one overwrite row.
Bits = tuple[int, int]


class RoleLike(Protocol):
    id: int
    allow: int
    deny: int
    parent_id: int | None
    position: int
    is_default: bool


def _chain_from_root(role_id: int | None, roles: Mapping[int, RoleLike]) -> list[RoleLike]:
    chain: list[RoleLike] = []
    seen: set[int] = set()
    # The seen set stops a parent cycle in old data.
    while role_id is not None and role_id not in seen:
        role = roles.get(role_id)
        if role is None:
            break
        seen.add(role_id)
        chain.append(role)
        role_id = role.parent_id
    chain.reverse()
    return chain


def resolved(role_id: int, roles: Mapping[int, RoleLike]) -> tuple[int, int]:
    """What a role says after inheritance: the nearest set value wins, so a
    child's allow beats a parent's deny and the other way round."""
    allow = deny = 0
    for role in _chain_from_root(role_id, roles):
        own_allow = role.allow & ~role.deny
        allow = (allow & ~role.deny) | own_allow
        deny = (deny & ~own_allow) | role.deny
    return allow, deny


def apply_layer(mask: int, roles: Mapping[int, RoleLike], assigned_ids: Iterable[int]) -> int:
    """@everyone, then the assigned roles from lowest to highest position:
    the highest role that says anything about a bit decides it."""
    for role in roles.values():
        if role.is_default:
            allow, deny = resolved(role.id, roles)
            mask = (mask & ~deny) | allow
    assigned = [
        roles[role_id] for role_id in set(assigned_ids)
        if role_id in roles and not roles[role_id].is_default
    ]
    for role in sorted(assigned, key=lambda r: (r.position, r.id)):
        allow, deny = resolved(role.id, roles)
        mask = (mask | allow) & ~deny
    return mask


def member_mask(roles: Mapping[int, RoleLike], assigned_ids: Iterable[int]) -> Permission:
    return Permission(apply_layer(0, roles, assigned_ids) & ALL_PERMISSIONS)


class _Overwritten:
    """A role as one channel or category sees it: its own place in the
    hierarchy, with that target's overwrite in place of its permissions."""

    def __init__(self, role: RoleLike, bits: Bits) -> None:
        self.id = role.id
        self.parent_id = role.parent_id
        self.position = role.position
        self.is_default = role.is_default
        self.allow, self.deny = bits


def overwrite_layer(
    mask: int,
    roles: Mapping[int, RoleLike],
    assigned_ids: Iterable[int],
    role_rows: Mapping[int, Bits],
    member_row: Bits | None,
) -> int:
    """One target's overwrites on top of *mask*: its role rows by the role rule
    (a role without a row inherits its parent's row), then the member's own row."""
    if role_rows:
        views = {rid: _Overwritten(role, role_rows.get(rid, (0, 0))) for rid, role in roles.items()}
        mask = apply_layer(mask, views, assigned_ids)
    if member_row is not None:
        allow, deny = member_row
        mask = (mask & ~deny) | allow
    return mask


def channel_mask(
    server_mask: int,
    roles: Mapping[int, RoleLike],
    assigned_ids: Iterable[int],
    layers: Iterable[tuple[Mapping[int, Bits], Bits | None]],
) -> Permission:
    """A member's mask in a channel: the server mask, then the category's and
    the channel's overwrites in that order. Only channel bits move, and without
    View none of them is left."""
    assigned_ids = list(assigned_ids)
    mask = int(server_mask)
    for role_rows, member_row in layers:
        mask = overwrite_layer(mask, roles, assigned_ids, role_rows, member_row)
    mask = (int(server_mask) & ~CHANNEL_BITS) | (mask & CHANNEL_BITS)
    if not mask & Permission.VIEW_CHANNEL:
        mask &= ~CHANNEL_BITS
    return Permission(mask & ALL_PERMISSIONS)


def descendants(role_id: int, roles: Mapping[int, RoleLike]) -> set[int]:
    children: dict[int, list[int]] = {}
    for role in roles.values():
        if role.parent_id is not None:
            children.setdefault(role.parent_id, []).append(role.id)
    found: set[int] = set()
    pending = [role_id]
    while pending:
        for child in children.get(pending.pop(), []):
            if child != role_id and child not in found:
                found.add(child)
                pending.append(child)
    return found


def would_cycle(role_id: int, parent_id: int | None, roles: Mapping[int, RoleLike]) -> bool:
    """Whether making *parent_id* the parent of *role_id* closes a loop."""
    seen: set[int] = set()
    while parent_id is not None and parent_id not in seen:
        if parent_id == role_id:
            return True
        seen.add(parent_id)
        parent = roles.get(parent_id)
        parent_id = parent.parent_id if parent else None
    return False
