"""Small shared settings that more than one module needs."""
import os
import re

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

# Regex for origins allowed in addition to the explicit list above (ZET-59).
# Vercel branch previews get randomized subdomains, so they can't be enumerated
# in ALLOWED_ORIGINS — we match them by pattern instead. Env-driven so each
# environment opts in; empty (the default) disables it entirely.
# SECURITY: anchor the pattern on both the project prefix and the team-slug
# suffix (e.g. ^https://ping-frontend-[a-z0-9-]+-vargakings-projects\.vercel\.app$).
# A loose pattern like ^https://.*\.vercel\.app$ would let anyone's Vercel
# deployment reach the API with credentials.
_raw_origin_regex = os.getenv("ALLOWED_ORIGIN_REGEX", "").strip()
ALLOWED_ORIGIN_REGEX: str | None = _raw_origin_regex or None
_origin_regex = re.compile(ALLOWED_ORIGIN_REGEX) if ALLOWED_ORIGIN_REGEX else None


def is_origin_allowed(origin: str) -> bool:
    """True if `origin` is in the explicit allowlist or matches the regex.

    Mirrors the CORSMiddleware's allow_origins/allow_origin_regex logic so the
    /ws handshake (which CORS never sees) accepts exactly the same origins.
    """
    if origin in ALLOWED_ORIGINS:
        return True
    return _origin_regex is not None and _origin_regex.fullmatch(origin) is not None
