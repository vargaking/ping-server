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
from .channels import ChannelResponse, _broadcast

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
    """Categories and channels from one read, so they can't disagree."""
    groups = await ChannelGroup.filter(server_id=server.id).order_by("position", "id")
    channels = {
        channel.id: channel for channel in await Channel.filter(server_id=server.id)}
    state = await read_state.batch_channel_state(current_user.id, list(channels))
    layout = await channel_layout.get_layout(server.id)
    return ChannelsSnapshot(
        groups=[GroupResponse(**channel_layout.group_json(group)) for group in groups],
        channels=[
            ChannelResponse.from_channel(
                channels[channel_id],
                last_read_message_id=state.get(channel_id, {}).get("last_read_message_id"),
                last_message_id=state.get(channel_id, {}).get("last_message_id"),
            )
            for channel_id in channel_layout.flatten(layout)
        ],
    )


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
    payload = channel_layout.group_json(group)
    await _broadcast(request, server.id, {
        "type": "channel_group_created",
        "server_id": server.id,
        "group": payload,
    }, exclude_user_id=current_user.id)
    return payload


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
    payload = channel_layout.group_json(group)
    await _broadcast(request, server.id, {
        "type": "channel_group_updated",
        "server_id": server.id,
        "group": payload,
    }, exclude_user_id=current_user.id)
    return payload


@router.delete(
    "/{server_id}/channel-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    """Delete a category. Its channels move to the end of the ungrouped list."""
    async with channel_layout.locked_server(server.id):
        group = await _group_in_server(server.id, group_id)
        orphans = await Channel.filter(group_id=group.id).order_by("position", "id")
        start = await channel_layout.next_channel_position(server.id, None)
        for offset, channel in enumerate(orphans):
            await Channel.filter(id=channel.id).update(group_id=None, position=start + offset)
        await group.delete()
        layout = await channel_layout.get_layout(server.id)
    await _broadcast(request, server.id, {
        "type": "channel_group_deleted",
        "server_id": server.id,
        "group_id": group_id,
        "layout": layout,
    }, exclude_user_id=current_user.id)


@router.put("/{server_id}/channel-layout", response_model=LayoutResponse)
async def set_layout(
    body: LayoutBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(_manage_channels()),
):
    layout = await channel_layout.apply_layout(server.id, body)
    await _broadcast(request, server.id, {
        "type": "channel_layout_updated",
        "server_id": server.id,
        "layout": layout,
    }, exclude_user_id=current_user.id)
    return layout
