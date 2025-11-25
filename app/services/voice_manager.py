from fastapi import WebSocket
from app.services.connection_manager import ConnectionManager

class VoiceManager:
    def __init__(self, connection_manager: ConnectionManager):
        self.connection_manager = connection_manager
        # Track voice participants: {channel_id: {user_id: user_data}}
        self.voice_channels = {}
        # Track producers: {channel_id: {user_id: producer_id}}
        self.channel_producers = {}

    async def handle_voice_join(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        server_id = message.get("server_id")
        
        # Fetch user details
        from app.models.User import User
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
                        print(f"Sent existing producer {producer_id} from user {other_user_id} to new joiner {user_id}")

        # Notify others in the server that someone joined
        from app.models.UserToServer import UserToServer
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

    async def handle_voice_leave(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        server_id = message.get("server_id")

        await self._remove_user_from_voice(user_id, channel_id, server_id)

    async def _remove_user_from_voice(self, user_id, channel_id, server_id=None):
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
            from app.models.UserToServer import UserToServer
            users_in_server = await UserToServer.filter(
                server_id=server_id).values_list('user_id', flat=True)
            for uid in users_in_server:
                ws = self.connection_manager.get_websocket(uid)
                if ws and uid != user_id:
                    await ws.send_json(broadcast_msg)
        else:
             # Fallback: notify just the people in the channel (if we knew who they were, but we just removed the user)
             # Ideally we should always have server_id.
             pass

    async def handle_producer_created(self, message: dict, websocket: WebSocket):
        """Broadcast new producer to other users in the same voice channel"""
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        producer_id = message.get("producer_id")
        
        print(f"Producer created by user {user_id} in channel {channel_id}: {producer_id}")
        
        # Store producer
        if channel_id not in self.channel_producers:
            self.channel_producers[channel_id] = {}
        self.channel_producers[channel_id][user_id] = producer_id
        
        # Get all users in this voice channel
        if channel_id not in self.voice_channels:
            print(f"Warning: Channel {channel_id} not found in voice_channels")
            return
            
        voice_users = self.voice_channels[channel_id].keys()
        
        broadcast_msg = {
            "type": "new_producer",
            "producerId": producer_id,
            "userId": str(user_id),
            "channelId": channel_id
        }
        
        print(f"Broadcasting new_producer to {len(voice_users)} users in channel {channel_id}")
        
        # Notify all users in the voice channel (except the producer)
        for uid in voice_users:
            if uid != user_id:
                ws = self.connection_manager.get_websocket(uid)
                if ws:
                    await ws.send_json(broadcast_msg)
                    print(f"  -> Sent to user {uid}")

    async def handle_voice_signal(self, message: dict, websocket: WebSocket):
        # Relay signaling messages (offer, answer, ice-candidate, etc.)
        # Target user is usually specified in the message
        target_user_id = message.get("target_user_id")
        
        if target_user_id:
            target_ws = self.connection_manager.get_websocket(target_user_id)
            if target_ws:
                # Forward the message exactly as is
                await target_ws.send_json(message)

    async def cleanup_user(self, user_id):
        # Clean up voice channels and notify others
        for channel_id in list(self.voice_channels.keys()):
            if user_id in self.voice_channels[channel_id]:
                # Notify others in this channel before removing
                # We might not know server_id here easily without looking it up or storing it.
                # For now, let's try to broadcast to the channel participants.
                
                # We need to find server_id to do a proper broadcast if we want to follow the pattern
                # But for now, let's just remove and notify channel members.
                
                # Actually, the original code broadcasted to the whole server? 
                # "Notify others in this channel before removing" -> _broadcast_user_left_voice
                
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

    async def _broadcast_user_left_voice(self, user_id, channel_id):
        """Helper to broadcast user_left_voice to all connected users in voice channel"""
        if channel_id not in self.voice_channels:
            return
            
        broadcast_msg = {
            "type": "user_left_voice",
            "user_id": user_id,
            "channel_id": channel_id
        }
        
        print(f"Broadcasting user_left_voice for user {user_id} in channel {channel_id}")
        
        # Notify all remaining users in the voice channel
        for uid in list(self.voice_channels[channel_id].keys()):
            if uid != user_id:
                ws = self.connection_manager.get_websocket(uid)
                if ws:
                    await ws.send_json(broadcast_msg)
                    print(f"  -> Notified user {uid}")
