from typing import Iterable, Mapping, Protocol

from ..permissions import ALL_PERMISSIONS, Permission


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


def member_mask(roles: Mapping[int, RoleLike], assigned_ids: Iterable[int]) -> Permission:
    """@everyone's allow, then the assigned roles from lowest to highest
    position: the highest role that says anything about a bit decides it."""
    mask = 0
    for role in roles.values():
        if role.is_default:
            mask |= resolved(role.id, roles)[0]
    assigned = [
        roles[role_id] for role_id in set(assigned_ids)
        if role_id in roles and not roles[role_id].is_default
    ]
    for role in sorted(assigned, key=lambda r: (r.position, r.id)):
        allow, deny = resolved(role.id, roles)
        mask = (mask | allow) & ~deny
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
