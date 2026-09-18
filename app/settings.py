"""Small shared settings that more than one module needs."""
import os

# Browser origins allowed to call the API with credentials. Used by the CORS
# middleware *and* by the WebSocket handshake: WebSockets are not covered by
# CORS, so the /ws endpoint has to check the Origin header itself.
ALLOWED_ORIGINS: list[str] = [
    origin
    for origin in (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://192.168.1.249:5173",
        "https://192.168.1.84:5173",
        "https://dpkchat.vercel.app",
        os.getenv("DEV_IP", ""),
    )
    if origin
]
