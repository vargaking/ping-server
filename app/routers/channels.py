import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Channel import Channel
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer

logger = logging.getLogger("app.routers.channels")

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_settings: dict
    type: str

    @classmethod
    def from_channel(cls, channel: Channel):
        return cls(
            id=channel.id,
            name=channel.name,
            channel_settings=channel.channel_settings,
            type=channel.type,
        )


async def _get_server_for_member(
    server_id: int, user_id: int
) -> Server:
    """Return the server if it exists and the user is a member, else raise 404."""
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found",
        )

    user_server_relation = await UserToServer.filter(
        user_id=user_id, server_id=server.id
    ).first()
    if not user_server_relation:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this server",
        )

    return server


@router.get("/messages")
async def get_messages(
    last_updated: datetime,
    current_user: User = Depends(get_current_user),
):
    """Fetch messages from servers the user belongs to, updated after last_updated."""
    user_servers = await UserToServer.filter(user=current_user).values_list(
        "server_id",
        flat=True,
    )

    messages = await Message.filter(
        server_id__in=user_servers,
        created_at__gt=last_updated,
    ).order_by("created_at").values(
        "uuid",
        "content",
        "author_id",
        "channel_id",
        "server_id",
        "timestamp",
    )

    return [
        {
            "id": message["uuid"],
            "content": message["content"],
            "user_id": message["author_id"],
            "channel_id": message["channel_id"],
            "server_id": message["server_id"],
            "timestamp": message["timestamp"],
        }
        for message in messages
    ]


@router.get("/{server_id}", response_model=List[ChannelResponse])
async def get_channels(
    server_id: int,
    current_user: User = Depends(get_current_user),
):
    server = await _get_server_for_member(server_id, current_user.id)

    channels = await Channel.filter(server_id=server.id).all()
    return [ChannelResponse.from_channel(channel) for channel in channels]


@router.post("/{server_id}/create", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    server_id: int,
    channel_name: str,
    channel_type: str = "text",
    current_user: User = Depends(get_current_user),
):
    server = await _get_server_for_member(server_id, current_user.id)

    channel = await Channel.create(
        name=channel_name,
        channel_settings={},
        type=channel_type,
        server=server,
    )

    return ChannelResponse.from_channel(channel)
