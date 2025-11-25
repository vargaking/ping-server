from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.middleware import get_current_user
from app.models import ChannelToServer
from app.models.Channel import Channel
from app.models.User import User
from app.models.Server import Server
from app.models.UserToServer import UserToServer
from app.models.Message import Message
from datetime import datetime

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
            type=channel.type
        )


@router.get("/{server_id}")
async def get_channels(server_id: int, current_user: User = Depends(get_current_user)):
    # Get server
    server = await Server.get_or_none(id=server_id)

    # Check if user is part of the server
    user_server_relation = await UserToServer.filter(user_id=current_user.id, server_id=server.id).first()

    if not server or not user_server_relation:
        return {"error": "Server not found"}

    channels_to_server = await ChannelToServer.filter(server_id=server.id).prefetch_related("channel")

    channels = [relation.channel for relation in channels_to_server]
    return [ChannelResponse.from_channel(channel) for channel in channels]


@router.post("/{server_id}/create", response_model=ChannelResponse)
async def create_channel(server_id: int, channel_name: str, type: str = "text", current_user: User = Depends(get_current_user)):
    # Get server
    server = await Server.get_or_none(id=server_id)

    # Check if user is part of the server
    user_server_relation = await UserToServer.filter(user_id=current_user.id, server_id=server.id).first()

    if not server or not user_server_relation:
        return {"error": "Server not found"}

    # Create channel
    channel = await Channel.create(
        name=channel_name,
        channel_settings={},
        type=type
    )

    # Link channel to server
    await ChannelToServer.create(
        channel=channel,
        server=server
    )

    return ChannelResponse.from_channel(channel)


@router.get("/messages/{last_updated}")
async def get_messages(
    last_updated: datetime,
    current_user: User = Depends(get_current_user)
):
    """Fetch messages from servers the user belongs to, updated after last_updated."""
    # Get servers the user belongs to
    user_servers = await UserToServer.filter(user=current_user).values_list(
        'server_id',
        flat=True
    )

    # Fetch messages from those servers updated after last_updated
    messages = await Message.filter(
        server_id__in=user_servers,
        created_at__gt=last_updated
    ).order_by('created_at').values(
        'uuid',
        'content',
        'author_id',
        'channel_id',
        'server_id',
        'timestamp'
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
