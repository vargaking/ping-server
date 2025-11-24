from datetime import datetime
from fastapi import WebSocket

from app.models.UserToServer import UserToServer
from app.models.Message import Message


class Communication:
    def __init__(self):
        self.user_to_websocket = {}
        self.websocket_to_user = {}
        # Track voice participants: {channel_id: {user_id: user_data}}
        self.voice_channels = {}
        # Track producers: {channel_id: {user_id: producer_id}}
        self.channel_producers = {}

    def add_connection(self, user_id, websocket):
        self.user_to_websocket[user_id] = websocket
        self.websocket_to_user[websocket] = user_id

    async def remove_connection_by_user_id(self, user_id):
        if user_id in self.user_to_websocket:
            websocket = self.user_to_websocket[user_id]
            del self.user_to_websocket[user_id]
            del self.websocket_to_user[websocket]
            
            # Clean up voice channels and notify others
            for channel_id in list(self.voice_channels.keys()):
                if user_id in self.voice_channels[channel_id]:
                    # Notify others in this channel before removing
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

    async def remove_connection_by_websocket(self, websocket: WebSocket):
        if websocket in self.websocket_to_user:
            user_id = self.websocket_to_user[websocket]
            del self.websocket_to_user[websocket]
            del self.user_to_websocket[user_id]
            
            # Clean up voice channels and notify others
            for channel_id in list(self.voice_channels.keys()):
                if user_id in self.voice_channels[channel_id]:
                    # Notify others in this channel before removing
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
            if uid in self.user_to_websocket and uid != user_id:
                await self.user_to_websocket[uid].send_json(broadcast_msg)
                print(f"  -> Notified user {uid}")

    async def forward_message(self, message: dict, server_id: str, channel_id: str):
        # get users in the server
        users_in_server = await UserToServer.filter(
            server_id=server_id).values_list('user_id', flat=True)

        for user_id in users_in_server:
            if user_id in self.user_to_websocket and user_id != message.get("user_id"):
                websocket = self.user_to_websocket[user_id]
                await websocket.send_json(message)

        # Save message to database
        await Message.create(
            uuid=message.get("id"),
            content=message.get("content"),
            author_id=message.get("user_id"),
            server_id=server_id,
            channel_id=channel_id,
            timestamp=message.get("timestamp"),
            metadata=message.get("metadata", {})
        )

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
        if user_id in self.user_to_websocket:
            await self.user_to_websocket[user_id].send_json({
                "type": "voice_participants",
                "channel_id": channel_id,
                "participants": existing_participants
            })
            
            # Also send existing producers
            if channel_id in self.channel_producers:
                for other_user_id, producer_id in self.channel_producers[channel_id].items():
                    if other_user_id != user_id:
                        await self.user_to_websocket[user_id].send_json({
                            "type": "new_producer",
                            "producerId": producer_id,
                            "userId": str(other_user_id),
                            "channelId": channel_id
                        })
                        print(f"Sent existing producer {producer_id} from user {other_user_id} to new joiner {user_id}")

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
            if uid in self.user_to_websocket and uid != user_id:
                ws = self.user_to_websocket[uid]
                await ws.send_json(broadcast_msg)

    async def handle_voice_leave(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        channel_id = message.get("channel_id")
        server_id = message.get("server_id")

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

        users_in_server = await UserToServer.filter(
            server_id=server_id).values_list('user_id', flat=True)

        broadcast_msg = {
            "type": "user_left_voice",
            "user_id": user_id,
            "channel_id": channel_id,
            "server_id": server_id
        }

        for uid in users_in_server:
            if uid in self.user_to_websocket and uid != user_id:
                ws = self.user_to_websocket[uid]
                await ws.send_json(broadcast_msg)

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
            if uid != user_id and uid in self.user_to_websocket:
                ws = self.user_to_websocket[uid]
                await ws.send_json(broadcast_msg)
                print(f"  -> Sent to user {uid}")

    async def handle_voice_signal(self, message: dict, websocket: WebSocket):
        # Relay signaling messages (offer, answer, ice-candidate, etc.)
        # Target user is usually specified in the message
        target_user_id = message.get("target_user_id")
        
        if target_user_id and target_user_id in self.user_to_websocket:
            target_ws = self.user_to_websocket[target_user_id]
            # Forward the message exactly as is
            await target_ws.send_json(message)

    async def message_switch(self, message: dict, websocket: WebSocket):
        user_id = message.get("user_id")
        server_id = message.get("server_id")
        channel_id = message.get("channel_id")

        message_type = message.get("type")

        if message_type == "connection_init":
            self.add_connection(user_id, websocket)
        elif message_type == "disconnect":
            self.remove_connection_by_user_id(user_id)
        elif message_type == "message":
            await self.forward_message(message, server_id, channel_id)
        elif message_type == "join_voice":
            await self.handle_voice_join(message, websocket)
        elif message_type == "leave_voice":
            await self.handle_voice_leave(message, websocket)
        elif message_type == "producer_created":
            await self.handle_producer_created(message, websocket)
        elif message_type == "voice_signal":
            await self.handle_voice_signal(message, websocket)
