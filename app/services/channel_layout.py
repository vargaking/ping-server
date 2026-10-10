from collections import Counter
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt
from tortoise.functions import Max
from tortoise.transactions import in_transaction

from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.Server import Server

DEFAULT_LAYOUT = (
    ("Text channels", "general", "text"),
    ("Voice channels", "General", "voice"),
)
MAX_CHANNELS = 500
MAX_GROUPS = 200
MAX_LISTED_IDS = 20


class GroupLayout(BaseModel):
    id: StrictInt
    channel_ids: list[StrictInt] = Field(max_length=MAX_CHANNELS)


class LayoutBody(BaseModel):
    ungrouped: list[StrictInt] = Field(max_length=MAX_CHANNELS)
    groups: list[GroupLayout] = Field(max_length=MAX_GROUPS)


@asynccontextmanager
async def locked_server(server_id: int):
    """A transaction holding the server's row lock, so layout checks and writes
    can't interleave with channel or group changes. The lock is a no-op on SQLite."""
    async with in_transaction():
        await Server.select_for_update().get(id=server_id)
        yield


def group_json(group: ChannelGroup) -> dict:
    return {
        "id": group.id,
        "server_id": group.server_id,
        "name": group.name,
        "position": group.position,
    }


async def seed_default_layout(server: Server) -> None:
    for position, (group_name, channel_name, channel_type) in enumerate(DEFAULT_LAYOUT):
        group = await ChannelGroup.create(server=server, name=group_name, position=position)
        await Channel.create(
            server=server, group=group, name=channel_name, type=channel_type,
            channel_settings={}, position=0)


async def next_channel_position(server_id: int, group_id: Optional[int]) -> int:
    top = await Channel.filter(server_id=server_id, group_id=group_id).annotate(
        top=Max("position")).values_list("top", flat=True)
    return 0 if not top or top[0] is None else top[0] + 1


async def next_group_position(server_id: int) -> int:
    top = await ChannelGroup.filter(server_id=server_id).annotate(
        top=Max("position")).values_list("top", flat=True)
    return 0 if not top or top[0] is None else top[0] + 1


def layout_of(channels: Iterable[Channel], groups: Iterable[ChannelGroup]) -> dict:
    """The layout of channels and categories already in (position, id) order."""
    by_group: dict[Optional[int], list[int]] = {}
    for channel in channels:
        by_group.setdefault(channel.group_id, []).append(channel.id)
    return {
        "ungrouped": by_group.get(None, []),
        "groups": [
            {"id": group.id, "channel_ids": by_group.get(group.id, [])}
            for group in groups
        ],
    }


async def get_layout(server_id: int) -> dict:
    groups = await ChannelGroup.filter(server_id=server_id).order_by("position", "id")
    channels = await Channel.filter(server_id=server_id).order_by("position", "id")
    return layout_of(channels, groups)


def flatten(layout: dict) -> list[int]:
    order = list(layout["ungrouped"])
    for group in layout["groups"]:
        order.extend(group["channel_ids"])
    return order


async def legacy_channel_orders(server_ids: list[int]) -> dict[int, list[int]]:
    """The flattened layout per server, in the shape older clients read
    from server_settings.channel_order."""
    groups = await ChannelGroup.filter(server_id__in=server_ids).values_list(
        "id", "server_id", "position")
    rank = {group_id: (position, group_id) for group_id, _, position in groups}
    channels = await Channel.filter(server_id__in=server_ids).values_list(
        "id", "server_id", "group_id", "position")
    orders: dict[int, list[tuple]] = {server_id: [] for server_id in server_ids}
    for channel_id, server_id, group_id, position in channels:
        key = (group_id is not None, rank.get(group_id, (0, 0)), position, channel_id)
        orders[server_id].append((key, channel_id))
    return {
        server_id: [channel_id for _, channel_id in sorted(entries)]
        for server_id, entries in orders.items()
    }


def _mismatch(kind: str, expected: set[int], given: list[int]) -> Optional[str]:
    counts = Counter(given)
    problems = {
        "missing": sorted(expected - set(counts)),
        "unknown": sorted(set(counts) - expected),
        "duplicated": sorted(i for i, n in counts.items() if n > 1),
    }
    parts = []
    for label, ids in problems.items():
        if ids:
            shown = ", ".join(map(str, ids[:MAX_LISTED_IDS]))
            parts.append(f"{label} {kind} ids: {shown}{', ...' if len(ids) > MAX_LISTED_IDS else ''}")
    return "; ".join(parts) or None


async def apply_layout(
    server_id: int, body: LayoutBody, visible: Optional[set[int]] = None
) -> dict:
    """Validate *body* against the server's channels and groups and renumber
    positions to match. Raises 422 unless it names each exactly once.

    With *visible*, the body names only those channels; the others keep
    their category and come after the named ones, in their old order."""
    async with locked_server(server_id):
        every_channel = await Channel.filter(server_id=server_id).order_by("position", "id")
        hidden = [c for c in every_channel if visible is not None and c.id not in visible]
        hidden_ids = {c.id for c in hidden}
        channels = {c.id: c for c in every_channel if c.id not in hidden_ids}
        groups = {
            group.id: group for group in await ChannelGroup.filter(server_id=server_id)}

        channel_ids = list(body.ungrouped)
        for group in body.groups:
            channel_ids.extend(group.channel_ids)
        problems = [
            _mismatch("channel", set(channels), channel_ids),
            _mismatch("group", set(groups), [group.id for group in body.groups]),
        ]
        problems = [problem for problem in problems if problem]
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))

        for position, group in enumerate(body.groups):
            if groups[group.id].position != position:
                await ChannelGroup.filter(id=group.id).update(position=position)

        targets = [(None, list(body.ungrouped))] + [(g.id, list(g.channel_ids)) for g in body.groups]
        for channel in hidden:
            channels[channel.id] = channel
            for group_id, ids in targets:
                if group_id == channel.group_id:
                    ids.append(channel.id)
        for group_id, ids in targets:
            for position, channel_id in enumerate(ids):
                channel = channels[channel_id]
                if channel.group_id != group_id or channel.position != position:
                    await Channel.filter(id=channel_id).update(
                        group_id=group_id, position=position)

        return await get_layout(server_id)
