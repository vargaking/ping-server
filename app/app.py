from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise
from dotenv import load_dotenv
import os

import uvicorn
from app.communication import Communication
from app.models import User
from app.routers import users, servers, auth, channels
from app.utils import call_media_server, lifespan
from .middleware import auth_middleware, get_current_user

load_dotenv()
# Initialize application logging (configures file logging)
from . import logging_config  # noqa: F401

app = FastAPI(debug=True, lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", os.getenv("DEV_IP", "")],
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


@app.get("/router_caps")
async def get_router_capabilities(request: Request):
    # We pass the Request object to access the shared httpx client instance
    caps = await call_media_server(
        request,
        method="GET",
        endpoint="/router_capabilities"
    )
    return caps

# --- Step 1: Create a Transport for Sending (Client A) ---


@app.post("/test/transport/send")
async def create_send_transport_test(request: Request):
    # Call Node.js server to create a sending transport
    return await call_media_server(request, "POST", "/create_transport", {"is_sending": True})

# --- Step 2: Create Producer (Client A) ---


@app.post("/test/produce")
async def produce_test(request: Request, data: dict):
    # data expects: {"transportId": ..., "kind": "audio", "rtpParameters": ...}
    # Call Node.js server to create a Producer
    return await call_media_server(request, "POST", "/produce", data)

# --- Step 3: Create a Transport for Receiving (Client B) ---


@app.post("/test/transport/receive")
async def create_receive_transport_test(request: Request):
    # Call Node.js server to create a receiving transport
    return await call_media_server(request, "POST", "/create_transport", {"is_sending": False})

# --- Step 4: Create Consumer (Client B) ---


@app.post("/test/consume")
async def consume_test(request: Request, data: dict):
    # data expects: {"transportId": B's ID, "producerId": A's ID}
    # Call Node.js server to create a Consumer
    return await call_media_server(request, "POST", "/consume", data)


@app.post("/api/media/connect_transport")
async def connect_transport_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/connect_transport", data)


@app.get("/api/media/router_capabilities")
async def get_router_capabilities_proxy(request: Request):
    return await call_media_server(request, "GET", "/router_capabilities")


@app.post("/api/media/create_transport")
async def create_transport_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/create_transport", data)


@app.post("/api/media/produce")
async def produce_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/produce", data)


@app.post("/api/media/consume")
async def consume(request: Request):
    return await call_media_server(request, "POST", "/consume", await request.json())


@app.post("/api/media/resume_consumer")
async def resume_consumer(request: Request):
    return await call_media_server(request, "POST", "/resume_consumer", await request.json())

comms = Communication()


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
