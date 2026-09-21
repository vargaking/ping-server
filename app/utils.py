import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager

from .models.Token import Token

logger = logging.getLogger("app.utils")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan. Voice now runs on LiveKit, so there is no media
    proxy client to manage here anymore.

    On startup we prune expired session tokens. Tortoise is already initialised
    at this point (register_tortoise wraps this lifespan), but keep it guarded
    so a housekeeping hiccup can never block the app from starting.
    """
    try:
        deleted = await Token.filter(
            expires_at__not_isnull=True,
            expires_at__lt=datetime.now(timezone.utc),
        ).delete()
        if deleted:
            logger.info("Pruned %s expired session token(s) on startup", deleted)
    except Exception:
        logger.warning("Failed to prune expired tokens on startup", exc_info=True)

    yield


async def require_membership(user, server):
    from .models.UserToServer import UserToServer
    membership = await UserToServer.filter(user=user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")


def require_owner(user, server):
    """Enforce that *user* owns *server*; raise 403 otherwise.

    Ownership is stricter than membership: use this to gate destructive or
    server-wide writes (settings, deletion, invites) that only the owner may do.
    """
    if server.owner_id is None or server.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the server owner can do this")
