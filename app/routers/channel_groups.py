from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.ChannelGroup import ChannelGroup
from ..models.Server import Server
from ..models.User import User
from ..permissions import Permission, check_permission
from ..services import channel_layout, read_state
from ..services.channel_layout import LayoutBody
from ..services.channel_visibility import (
    filter_layout,
    group_payload,
    private_flags,
    send_layout,
    send_per_member,
    visibility_change,
)
from ..services.permissions import member_views, permissions
from .channels import ChannelResponse

router = APIRouter(prefix="/servers", tags=["channel groups"])

GROUP_NAME_MAX = 100


def _clean_group_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Category name can't be empty")
    if len(value) > GROUP_NAME_MAX:
        raise ValueError(f"Category name must be at most {GROUP_NAME_MAX} characters")
    return value


class GroupName(BaseModel):
    name: str

    _name = field_validator("name")(_clean_group_name)


class GroupResponse(BaseModel):
    id: int
    server_id: int
    name: str
    position: int
    private: bool = False


class ChannelsSnapshot(BaseModel):
    groups: List[GroupResponse]
    channels: List[ChannelResponse]


class GroupLayoutResponse(BaseModel):
    id: int
    channel_ids: List[int]


class LayoutResponse(BaseModel):
    ungrouped: List[int]
    groups: List[GroupLayoutResponse]


def _manage_channels():
    return check_permission(Permission.MANAGE_CHANNELS, hide=True)


async def _group_in_server(server_id: int, group_id: int) -> ChannelGroup:
    group = await ChannelGroup.get_or_none(id=group_id, server_id=server_id)
    if not group:
        raise HTTPException(status_code=404, detail="Category not found")
    return group


@router.get("/{server_id}/channels", response_model=ChannelsSnapshot)
async def get_channels_snapshot(
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, hide=True)),
):
    """Categories and channels from one read, so they can't disagree. Only
    what the caller can view is listed."""
    view = await permissions.view(current_user.id, server)
    groups = await ChannelGroup.filter(server_id=server.id).order_by("position", "id")
    channels = {
        channel.id: channel for channel in await Channel.filter(server_id=server.id)}
    visible = [cid for cid in channel_layout.flatten(
        await channel_layout.get_layout(server.id)) if view.can_view(cid)]
    state = await read_state.batch_channel_state(current_user.id, visible)
    private_channels, private_groups = await private_flags(server.id)
    return ChannelsSnapshot(
        groups=[
            GroupResponse(**group_payload(group, private_groups))
            for group in groups if group.id in view.groups],
        channels=[
            ChannelResponse.from_channel(
                channels[channel_id],
                last_read_message_id=state.get(channel_id, {}).get("last_read_message_id"),
                last_message_id=state.get(channel_id, {}).get("last_message_id"),
                private=channel_id in private_channels,
            )
            for channel_id in visible
        ],
    )


async def _send_group_frame(
    request: Request, server: Server, frame_type: str, group: ChannelGroup, exclude_user_id: int
) -> dict:
    """Send the group to the members it is shown to and return its payload."""
    _, private_groups = await private_flags(server.id)
    payload = group_payload(group, private_groups)
    await send_per_member(
        getattr(request.app.state, "comms", None), server,
        lambda user_id, view: None if user_id == exclude_user_id or group.id not in view.groups
        else {"type": frame_type, "server_id": server.id, "group": payload})
    return payload


@router.post(
    "/{server_id}/channel-groups", response_model=GroupResponse,
    status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupName,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    async with channel_layout.locked_server(server.id):
        if await ChannelGroup.filter(server_id=server.id).count() >= channel_layout.MAX_GROUPS:
            raise HTTPException(
                status_code=422,
                detail=f"A server can have at most {channel_layout.MAX_GROUPS} categories")
        group = await ChannelGroup.create(
            server=server,
            name=body.name,
            position=await channel_layout.next_group_position(server.id),
        )
    permissions.invalidate(server.id)
    return await _send_group_frame(
        request, server, "channel_group_created", group, current_user.id)


@router.patch("/{server_id}/channel-groups/{group_id}", response_model=GroupResponse)
async def rename_group(
    group_id: int,
    body: GroupName,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    group = await _group_in_server(server.id, group_id)
    group.name = body.name
    await group.save(update_fields=["name"])
    return await _send_group_frame(
        request, server, "channel_group_updated", group, current_user.id)


@router.delete(
    "/{server_id}/channel-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    """Delete a category. Its channels move to the end of the ungrouped list,
    and lose the category's permission overwrites."""
    shown_to = {user_id for user_id, view in (await member_views(server)).items()
                if group_id in view.groups and user_id != current_user.id}
    async with visibility_change(
            request.app.state, server, exclude_user_id=current_user.id):
        async with channel_layout.locked_server(server.id):
            group = await _group_in_server(server.id, group_id)
            orphans = await Channel.filter(group_id=group.id).order_by("position", "id")
            start = await channel_layout.next_channel_position(server.id, None)
            for offset, channel in enumerate(orphans):
                await Channel.filter(id=channel.id).update(
                    group_id=None, position=start + offset)
            await group.delete()
    await send_layout(
        getattr(request.app.state, "comms", None), server, "channel_group_deleted",
        {"group_id": group_id}, only=shown_to)


@router.put("/{server_id}/channel-layout", response_model=LayoutResponse)
async def set_layout(
    body: LayoutBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    view = await permissions.view(current_user.id, server)
    async with visibility_change(
            request.app.state, server, exclude_user_id=current_user.id):
        layout = await channel_layout.apply_layout(server.id, body, view.visible_channels())
    await send_layout(
        getattr(request.app.state, "comms", None), server, "channel_layout_updated",
        exclude_user_id=current_user.id)
    return filter_layout(layout, await permissions.view(current_user.id, server))
