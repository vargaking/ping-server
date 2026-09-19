"""Shared slowapi rate limiter.

A single in-process limiter is fine here: the app runs one Uvicorn worker, so
the in-memory counters are authoritative. ``headers_enabled=True`` makes the 429
responses carry ``Retry-After`` (and X-RateLimit-*).

Limits are read from the environment per request (see settings), so a test can
lower them without re-importing the app.
"""
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, headers_enabled=True)


def invite_use_key(request: Request) -> str:
    """Rate-limit invite redemption per client IP *and* per invite id, so one
    noisy IP can't brute-force a single invite's password and no single invite
    can be hammered from a pool of addresses."""
    invite_id = request.path_params.get("invite_id")
    return f"{get_remote_address(request)}:{invite_id}"
