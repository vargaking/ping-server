import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager
import httpx
from httpx import AsyncClient
from starlette.requests import Request

logger = logging.getLogger("app.utils")

MEDIA_SERVER_URL = os.getenv("MEDIA_SERVER_URL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create and tear down the shared httpx client for the media server proxy."""
    headers = {"x-api-key": os.getenv("MEDIA_SERVER_API_KEY", "")}
    app.requests_client = httpx.AsyncClient(base_url=MEDIA_SERVER_URL, headers=headers)
    yield
    await app.requests_client.aclose()


async def call_media_server(
    request: Request,
    method: str,
    endpoint: str,
    json_data: dict | None = None,
) -> dict:
    """Proxy a request to the Node.js Mediasoup media server."""
    client: AsyncClient = request.app.requests_client

    if method.upper() == "GET":
        response = await client.get(endpoint, timeout=5.0)
    elif method.upper() == "POST":
        response = await client.post(endpoint, json=json_data, timeout=5.0)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Media server error: %s %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise HTTPException(
            status_code=503,
            detail=f"Media server error: could not execute {endpoint}.",
        ) from exc

    if response.status_code == 204:
        return {}
    return response.json()


async def require_membership(user, server):
    from .models.UserToServer import UserToServer
    membership = await UserToServer.filter(user=user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")
