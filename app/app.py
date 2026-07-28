import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from tortoise.contrib.fastapi import register_tortoise

from app.communication import Communication
from app.db import TORTOISE_CONFIG
from app.routers import auth, channels, invites, servers, users, voice
from app.utils import lifespan
from .middleware import auth_middleware
# Initialize application logging (configures file logging)
from . import logging_config  # noqa: F401

logger = logging.getLogger("app")

app = FastAPI(debug=os.getenv("DEBUG", "").lower() == "true", lifespan=lifespan)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://192.168.1.249:5173",
        "https://192.168.1.84:5173",
        os.getenv("DEV_IP", ""),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication middleware
app.middleware("http")(auth_middleware)

# Routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(servers.router)
app.include_router(channels.router)
app.include_router(invites.router)
app.include_router(voice.router)

# Serve uploaded media from local disk (avatars, server icons).
# storage.py writes into MEDIA_ROOT and returns URLs prefixed with MEDIA_BASE_URL;
# this mount serves that same directory at MEDIA_MOUNT_PATH (the path portion of
# MEDIA_BASE_URL). For higher throughput, Nginx can serve MEDIA_ROOT directly.
_media_root = os.getenv("MEDIA_ROOT", "media")
_media_mount = os.getenv("MEDIA_MOUNT_PATH", "/media")
os.makedirs(_media_root, exist_ok=True)
app.mount(_media_mount, StaticFiles(directory=_media_root), name="media")

# Database
register_tortoise(
    app,
    config=TORTOISE_CONFIG,
    generate_schemas=False,
    add_exception_handlers=True,
)


@app.get("/")
async def root():
    return {"message": "Hello World"}


comms = Communication()
app.state.comms = comms


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            await comms.message_switch(data, websocket)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
        await comms.remove_connection_by_websocket(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.app:app", host="0.0.0.0", port=8000)
