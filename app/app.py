import logging
import os

import anyio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from tortoise.contrib.fastapi import register_tortoise

from app.communication import Communication
from app.db import TORTOISE_CONFIG
from app.rate_limit import limiter
from app.routers import auth, channels, invites, messages, servers, users, voice
from app.settings import ALLOWED_ORIGINS, ALLOWED_ORIGIN_REGEX, is_origin_allowed
from app.utils import lifespan
from .middleware import auth_middleware, resolve_user_from_token
# Initialize application logging (configures file logging)
from . import logging_config  # noqa: F401

logger = logging.getLogger("app")

app = FastAPI(debug=os.getenv("DEBUG", "").lower() == "true", lifespan=lifespan)

# Rate limiting: register the shared limiter and its 429 handler
# (which emits Retry-After). Individual endpoints opt in via @limiter.limit.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware. allow_origins is the explicit list; allow_origin_regex
# additionally matches Vercel branch previews, whose subdomains are randomized
# and so can't be enumerated (ZET-59). The regex comes from env (app/settings.py).
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
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
app.include_router(messages.router)
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
# Schemas are owned by Aerich migrations. DB_GENERATE_SCHEMAS=true is only for
# throwaway databases (the test suite, a scratch local SQLite file).
register_tortoise(
    app,
    config=TORTOISE_CONFIG,
    generate_schemas=os.getenv("DB_GENERATE_SCHEMAS", "").lower() == "true",
    add_exception_handlers=True,
)


@app.get("/")
async def root():
    return {"message": "Hello World"}


comms = Communication()
app.state.comms = comms


# Application-level close code: "no valid session". The frontend must not
# auto-reconnect on this one (it would loop forever); it should go to login.
WS_CLOSE_UNAUTHENTICATED = 4401


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # CORS does not apply to WebSockets and the session cookie is SameSite=None
    # in production, so without this check any website could open an
    # authenticated socket on behalf of a logged-in visitor (CSWSH).
    # Non-browser clients send no Origin header and carry no ambient cookies.
    origin = websocket.headers.get("origin")
    if origin is not None and not is_origin_allowed(origin):
        logger.warning("Rejected WebSocket from disallowed origin %s", origin)
        await websocket.close(code=1008)  # before accept() -> HTTP 403
        return

    user = await resolve_user_from_token(websocket.cookies.get("access_token"))

    # Accept first even when unauthenticated: a close code only reaches the
    # browser after the handshake completed.
    await websocket.accept()
    if user is None:
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED)
        return

    await comms.connect(user.id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await comms.message_switch(data, websocket)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected (user %s)", user.id)
    finally:
        # Also runs when a handler raises, so a crashed connection can't leave
        # a stale "online" entry behind. Shielded so that cancellation of this
        # task (server shutdown, the test client) can't interrupt the cleanup
        # halfway and skip the "went offline" broadcast.
        with anyio.CancelScope(shield=True):
            await comms.remove_connection_by_websocket(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.app:app", host="0.0.0.0", port=8000)
