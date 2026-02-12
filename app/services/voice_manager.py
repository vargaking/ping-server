import logging

from fastapi import WebSocket

from app.models.User import User
from app.models.UserToServer import UserToServer
from app.services.connection_manager import ConnectionManager

logger = logging.getLogger("app.services.voice_manager")


class VoiceManager:
    """Manages voice channel state and signaling between participants."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self.connection_manager = connection_manager
        self.voice_channels: dict[int, dict[int, dict]] = {}
        self.channel_producers: dict[int, dict[int, str]] = {}

    async def handle_voice_join(self, message: dict, websocket: WebSocket) -> None:
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        server_id = message.get("server_id")
        
        user = await User.get_or_none(id=user_id)
        user_data = {
            "id": user_id,
            "username": user.username if user else "Unknown",
            "profile": user.profile if user else {}
        }

        # Initialize channel if needed
        if channel_id not in self.voice_channels:
            self.voice_channels[channel_id] = {}

        # Get existing participants before adding new user
        existing_participants = list(self.voice_channels[channel_id].values())

        # Add user to voice channel
        self.voice_channels[channel_id][user_id] = user_data

        # Send existing participants to the new joiner
        if websocket:
            await websocket.send_json({
                "type": "voice_participants",
                "channel_id": channel_id,
                "participants": existing_participants
            })
            
            # Also send existing producers
            if channel_id in self.channel_producers:
                for other_user_id, producer_id in self.channel_producers[channel_id].items():
                    if other_user_id != user_id:
                        await websocket.send_json({
                            "type": "new_producer",
                            "producerId": producer_id,
                            "userId": str(other_user_id),
                            "channelId": channel_id
                        })
                        logger.debug("Sent existing producer %s from user %s to new joiner %s", producer_id, other_user_id, user_id)

        # Notify others in the server that someone joined
        users_in_server = await UserToServer.filter(
            server_id=server_id).values_list('user_id', flat=True)

        broadcast_msg = {
            "type": "user_joined_voice",
            "user_id": user_id,
            "channel_id": channel_id,
            "server_id": server_id,
            "user": user_data
        }

        for uid in users_in_server:
            ws = self.connection_manager.get_websocket(uid)
            if ws and uid != user_id:
                await ws.send_json(broadcast_msg)

    async def handle_voice_leave(self, message: dict, websocket: WebSocket) -> None:
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        server_id = message.get("server_id")

        await self._remove_user_from_voice(user_id, channel_id, server_id)

    async def _remove_user_from_voice(
        self,
        user_id: int,
        channel_id: int,
        server_id: int | None = None,
    ) -> None:
        # Remove from voice channel tracking
        if channel_id in self.voice_channels:
            self.voice_channels[channel_id].pop(user_id, None)
            # Clean up empty channels
            if not self.voice_channels[channel_id]:
                del self.voice_channels[channel_id]
        
        # Remove producer
        if channel_id in self.channel_producers:
            self.channel_producers[channel_id].pop(user_id, None)
            if not self.channel_producers[channel_id]:
                del self.channel_producers[channel_id]

        # If server_id is not provided, we can't broadcast to server, 
        # but we can broadcast to channel participants
        
        broadcast_msg = {
            "type": "user_left_voice",
            "user_id": user_id,
            "channel_id": channel_id,
            "server_id": server_id
        }

        if server_id:
            users_in_server = await UserToServer.filter(
                server_id=server_id).values_list('user_id', flat=True)
            for uid in users_in_server:
                ws = self.connection_manager.get_websocket(uid)
                if ws and uid != user_id:
                    await ws.send_json(broadcast_msg)
        else:
            pass  # Cannot broadcast without server_id

    async def handle_producer_created(self, message: dict, websocket: WebSocket) -> None:
        """Broadcast new producer to other users in the same voice channel"""
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        producer_id = message.get("producer_id")
        
        logger.info("Producer created by user %s in channel %s: %s", user_id, channel_id, producer_id)
        
        # Store producer
        if channel_id not in self.channel_producers:
            self.channel_producers[channel_id] = {}
        self.channel_producers[channel_id][user_id] = producer_id
        
        # Get all users in this voice channel
        if channel_id not in self.voice_channels:
            logger.warning("Channel %s not found in voice_channels", channel_id)
            return
            
        voice_users = self.voice_channels[channel_id].keys()
        
        broadcast_msg = {
            "type": "new_producer",
            "producerId": producer_id,
            "userId": str(user_id),
            "channelId": channel_id
        }
        
        logger.debug("Broadcasting new_producer to %d users in channel %s", len(list(voice_users)), channel_id)

        for uid in voice_users:
            if uid != user_id:
                ws = self.connection_manager.get_websocket(uid)
                if ws:
                    await ws.send_json(broadcast_msg)
                    logger.debug("Sent new_producer to user %s", uid)

    async def handle_voice_signal(self, message: dict, websocket: WebSocket) -> None:
        """Relay signaling messages (offer, answer, ICE candidates) to the target user."""
        target_user_id = message.get("target_user_id")
        
        if target_user_id:
            target_ws = self.connection_manager.get_websocket(target_user_id)
            if target_ws:
                # Forward the message exactly as is
                await target_ws.send_json(message)

    async def cleanup_user(self, user_id: int) -> None:
        """Remove a user from all voice channels and notify remaining participants."""
        for channel_id in list(self.voice_channels.keys()):
            if user_id in self.voice_channels[channel_id]:
                await self._broadcast_user_left_voice(user_id, channel_id)
                
                del self.voice_channels[channel_id][user_id]
                if not self.voice_channels[channel_id]:
                    del self.voice_channels[channel_id]
                    
        # Clean up producers
        for channel_id in list(self.channel_producers.keys()):
            if user_id in self.channel_producers[channel_id]:
                del self.channel_producers[channel_id][user_id]
                if not self.channel_producers[channel_id]:
                    del self.channel_producers[channel_id]

    async def _broadcast_user_left_voice(self, user_id: int, channel_id: int) -> None:
        """Notify remaining voice channel participants that a user left."""
        if channel_id not in self.voice_channels:
            return
            
        broadcast_msg = {
            "type": "user_left_voice",
            "user_id": user_id,
            "channel_id": channel_id
        }
        
        logger.debug("Broadcasting user_left_voice for user %s in channel %s", user_id, channel_id)
        
        # Notify all remaining users in the voice channel
        for uid in list(self.voice_channels[channel_id].keys()):
            if uid != user_id:
                ws = self.connection_manager.get_websocket(uid)
                if ws:
                    await ws.send_json(broadcast_msg)
                    logger.debug("Notified user %s of voice leave", uid)
