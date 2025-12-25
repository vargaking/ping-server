from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise
from dotenv import load_dotenv
import os

load_dotenv()

import uvicorn
from app.communication import Communication
from app.models import User
from app.routers import users, servers, auth, channels, media
from app.utils import call_media_server, lifespan
from .middleware import auth_middleware, get_current_user
# Initialize application logging (configures file logging)
from . import logging_config  # noqa: F401

app = FastAPI(debug=True, lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://192.168.1.249:5173",
        os.getenv("DEV_IP", "")
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add authentication middleware
app.middleware("http")(auth_middleware)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(servers.router)
app.include_router(channels.router)

TORTOISE_CONFIG = {
    "connections": {"default": os.getenv("DB_CONNECTION_STRING")},
    "apps": {
        "models": {
            "models": ["app.models"],
            "default_connection": "default",
        },
    },
}

register_tortoise(
    app,
    config=TORTOISE_CONFIG,
    # Set to True to auto-create tables (not recommended for production)
    generate_schemas=False,
    add_exception_handlers=True,
)


@app.get("/")
async def root():
    return {"message": "Hello World"}

app.include_router(media.router)

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
        print("WebSocket disconnected")
        await comms.remove_connection_by_websocket(websocket)

if __name__ == "__main__":
    # When running as a module (`python -m app.app`) or with uvicorn, use the module path
    # This avoids re-import issues when the package isn't on sys.path.
    uvicorn.run("app.app:app", host="0.0.0.0", port=8000)
