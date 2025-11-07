from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.middleware import get_current_user
from app.models import ChannelToServer
from app.models.Channel import Channel
from app.models.User import User
from app.models.Server import Server
from app.models.UserToServer import UserToServer

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_settings: dict

    @classmethod
    def from_channel(cls, channel: Channel):
        return cls(
            id=channel.id,
            name=channel.name,
            channel_settings=channel.channel_settings
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
async def create_channel(server_id: int, channel_name: str, current_user: User = Depends(get_current_user)):
    # Get server
    server = await Server.get_or_none(id=server_id)

    # Check if user is part of the server
    user_server_relation = await UserToServer.filter(user_id=current_user.id, server_id=server.id).first()

    if not server or not user_server_relation:
        return {"error": "Server not found"}

    # Create channel
    channel = await Channel.create(
        name=channel_name,
        channel_settings={}
    )

    # Link channel to server
    await ChannelToServer.create(
        channel=channel,
        server=server
    )

    return ChannelResponse.from_channel(channel)
