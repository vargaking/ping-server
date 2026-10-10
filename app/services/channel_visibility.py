from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Mapping

from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.Server import Server
from ..permissions import Permission
from . import channel_layout, read_state
from .permissions import MemberView, load_rules, member_views, permissions
from .server_queue import server_queue
from .voice_moderation import publish_sources, server_muted_attributes
from .voice_presence import remove_from_voice, voice_channels_in


def filter_layout(layout: dict, view: MemberView | None) -> dict:
    """The part of *layout* a member sees."""
    if view is None:
        return {"ungrouped": [], "groups": []}
    return {
        "ungrouped": [cid for cid in layout["ungrouped"] if view.can_view(cid)],
        "groups": [
            {"id": group["id"],
             "channel_ids": [cid for cid in group["channel_ids"] if view.can_view(cid)]}
            for group in layout["groups"] if group["id"] in view.groups
        ],
    }


def filter_order(order: list[int], view: MemberView | None) -> list[int]:
    return [] if view is None else [cid for cid in order if view.can_view(cid)]


def filter_settings(settings: dict | None, view: MemberView | None) -> dict:
    """Server settings without a default channel the member can't view."""
    settings = dict(settings or {})
    default = settings.get("default_channel_id")
    if isinstance(default, int) and (view is None or not view.can_view(default)):
        del settings["default_channel_id"]
    return settings


async def private_flags(server_id: int) -> tuple[set[int], set[int]]:
    """The channels and the categories @everyone can't view."""
    server = await Server.get(id=server_id)
    return (await load_rules(server)).private_flags()


@dataclass
class ServerState:
    """What visibility frames are built from, read once per fan-out."""
    channels: dict[int, Channel]
    groups: dict[int, ChannelGroup]
    layout: dict
    private_channels: set[int]
    private_groups: set[int]


async def load_state(server: Server) -> ServerState:
    channels = await Channel.filter(server_id=server.id).order_by("position", "id")
    groups = await ChannelGroup.filter(server_id=server.id).order_by("position", "id")
    private_channels, private_groups = (await load_rules(server)).private_flags()
    return ServerState(
        {channel.id: channel for channel in channels}, {group.id: group for group in groups},
        channel_layout.layout_of(channels, groups), private_channels, private_groups)


def group_payload(group: ChannelGroup, private_groups: set[int]) -> dict:
    return {**channel_layout.group_json(group), "private": group.id in private_groups}


async def send_per_member(comms, server: Server, build) -> None:
    """Send each connected member the frame *build(user_id, view)* returns,
    unless it returns None."""
    if comms is None:
        return
    for user_id, view in (await member_views(server)).items():
        frame = build(user_id, view)
        if frame is not None:
            await comms.send_to_user(user_id, frame)


async def send_layout(comms, server: Server, frame_type: str, extra: dict | None = None,
                      *, only: set[int] | None = None, exclude_user_id: int | None = None) -> None:
    """Send every member (or *only* those) their own filtered layout."""
    layout = await channel_layout.get_layout(server.id)

    def build(user_id: int, view: MemberView) -> dict | None:
        if user_id == exclude_user_id or (only is not None and user_id not in only):
            return None
        return {"type": frame_type, "server_id": server.id, **(extra or {}),
                "layout": filter_layout(layout, view)}

    await send_per_member(comms, server, build)


def permissions_frame(server_id: int, view: MemberView) -> dict:
    return {
        "type": "permissions_updated",
        "server_id": server_id,
        "permissions": str(int(view.mask)),
        "channels": view.differing_masks(),
    }


async def announce_visibility(
    app_state: Any, server_id: int, state: ServerState,
    before: Mapping[int, MemberView], after: Mapping[int, MemberView],
    *, exclude_user_id: int | None = None,
    flipped_channels: Collection[int] = (), flipped_groups: Collection[int] = (),
) -> None:
    """Tell every member what changed for them: channels and categories they
    lost or gained, their new masks, and the voice rooms they may no longer
    be in or speak in. *exclude_user_id* (the actor, who has the result from
    the response) gets only lost channels and mask changes. Channels and
    categories in *flipped_channels* / *flipped_groups* changed their private
    flag: those who keep seeing them get the new object."""
    from ..routers.channels import ChannelResponse

    comms = getattr(app_state, "comms", None)
    presence = getattr(app_state, "voice_presence", None)
    moderation = getattr(app_state, "voice_moderation", None)
    members = [(user_id, before[user_id], new) for user_id, new in after.items()
               if user_id in before]
    gained = {
        user_id: (set() if user_id == exclude_user_id
                  else (new.visible_channels() - old.visible_channels()) & set(state.channels))
        for user_id, old, new in members}
    read_states = {}
    if comms is not None:
        read_states = await read_state.batch_channel_states({
            user_id: sorted(ids) for user_id, ids in gained.items() if ids})
    channel_payloads: dict[int, dict] = {}
    group_payloads: dict[int, dict] = {}

    for user_id, old, new in members:
        actor = user_id == exclude_user_id
        lost = (old.visible_channels() - new.visible_channels()) & set(state.channels)
        lost_groups = (old.groups - new.groups) & set(state.groups)
        gained_groups = (new.groups - old.groups) & set(state.groups)

        if comms is not None:
            for channel_id in sorted(lost):
                await comms.send_to_user(user_id, {
                    "type": "channel_deleted", "server_id": server_id, "channel_id": channel_id,
                    "reason": "no_access"})
            for group_id in sorted(gained_groups) if not actor else []:
                await comms.send_to_user(user_id, {
                    "type": "channel_group_created", "server_id": server_id,
                    "group": group_payload(state.groups[group_id], state.private_groups)})
            for channel_id in sorted(gained[user_id]):
                payload = ChannelResponse.from_channel(
                    state.channels[channel_id],
                    last_read_message_id=read_states[user_id][channel_id]["last_read_message_id"],
                    last_message_id=read_states[user_id][channel_id]["last_message_id"],
                    private=channel_id in state.private_channels,
                )
                await comms.send_to_user(user_id, {
                    "type": "channel_created", "server_id": server_id,
                    "channel": payload.model_dump(mode="json")})
            mine = filter_layout(state.layout, new)
            for group_id in sorted(lost_groups) if not actor else []:
                await comms.send_to_user(user_id, {
                    "type": "channel_group_deleted", "server_id": server_id,
                    "group_id": group_id, "layout": mine})
            if old.mask != new.mask or old.differing_masks() != new.differing_masks():
                await comms.send_to_user(user_id, permissions_frame(server_id, new))
            for group_id in sorted(flipped_groups):
                if group_id in state.groups and group_id in old.groups and group_id in new.groups:
                    if group_id not in group_payloads:
                        group_payloads[group_id] = group_payload(
                            state.groups[group_id], state.private_groups)
                    await comms.send_to_user(user_id, {
                        "type": "channel_group_updated", "server_id": server_id,
                        "group": group_payloads[group_id]})
            for channel_id in sorted(flipped_channels):
                if channel_id in state.channels and old.can_view(channel_id) and new.can_view(channel_id):
                    if channel_id not in channel_payloads:
                        channel_payloads[channel_id] = ChannelResponse.from_channel(
                            state.channels[channel_id],
                            private=channel_id in state.private_channels).model_dump(
                            mode="json", exclude={"last_read_message_id", "last_message_id"})
                    await comms.send_to_user(user_id, {
                        "type": "channel_updated", "server_id": server_id,
                        "channel": channel_payloads[channel_id]})

        for channel_id in voice_channels_in(presence, state.channels, user_id):
            was = old.channels.get(channel_id, Permission(0))
            now = new.channels.get(channel_id, Permission(0))
            if not now & Permission.CONNECT:
                await remove_from_voice(presence, [channel_id], user_id)
            elif (was ^ now) & (Permission.SPEAK | Permission.STREAM):
                server_muted = moderation is not None and moderation.is_muted(server_id, user_id)
                await presence.update_participant(
                    channel_id, user_id, publish_sources(now, server_muted),
                    server_muted_attributes(server_muted))


class visibility_change:
    """``async with visibility_change(app_state, server):`` around a change
    that can move what members see. Masks are dropped and the difference is
    announced when the block ends."""

    def __init__(self, app_state: Any, server: Server, *, exclude_user_id: int | None = None) -> None:
        self.app_state = app_state
        self.server = server
        self.exclude_user_id = exclude_user_id

    async def __aenter__(self) -> "visibility_change":
        await server_queue.idle(self.server.id)
        self.before = await member_views(self.server)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            return
        permissions.invalidate(self.server.id)
        await announce_visibility(
            self.app_state, self.server.id, await load_state(self.server), self.before,
            await member_views(self.server), exclude_user_id=self.exclude_user_id)
