import logging

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager

logger = logging.getLogger("app.utils")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan. Voice now runs on LiveKit, so there is no media
    proxy client to manage here anymore."""
    yield


async def require_membership(user, server):
    from .models.UserToServer import UserToServer
    membership = await UserToServer.filter(user=user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")
