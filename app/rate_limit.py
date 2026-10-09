"""Shared slowapi rate limiter.

A single in-process limiter is fine here: the app runs one Uvicorn worker, so
the in-memory counters are authoritative. ``headers_enabled=True`` makes the 429
responses carry ``Retry-After`` (and X-RateLimit-*).

Limits are read from the environment per request (see settings), so a test can
lower them without re-importing the app.
"""
import hmac
import ipaddress

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .settings import trusted_proxy_key

CLIENT_IP_HEADER = "x-zet-client-ip"
PROXY_KEY_HEADER = "x-zet-proxy-key"


def client_address(request: Request) -> str:
    """The address a request came from. Behind the Cloudflare Worker every request
    connects from a Cloudflare address, so the Worker's header is used instead, but
    only when it carries the shared key."""
    key = trusted_proxy_key()
    sent_key = request.headers.get(PROXY_KEY_HEADER)
    if key and sent_key is not None and hmac.compare_digest(sent_key.encode(), key.encode()):
        try:
            return str(ipaddress.ip_address(request.headers.get(CLIENT_IP_HEADER, "").strip()))
        except ValueError:
            pass
    return get_remote_address(request)


limiter = Limiter(key_func=client_address, headers_enabled=True)


def invite_use_key(request: Request) -> str:
    """Rate-limit invite redemption per client IP *and* per invite id, so one
    noisy IP can't brute-force a single invite's password and no single invite
    can be hammered from a pool of addresses."""
    invite_id = request.path_params.get("invite_id")
    return f"{client_address(request)}:{invite_id}"


def user_key(request: Request) -> str:
    """Rate-limit per signed-in user, falling back to the client IP."""
    user = getattr(request.state, "user", None)
    return f"user:{user.id}" if user else client_address(request)
