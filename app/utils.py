from fastapi.concurrency import asynccontextmanager
import httpx
from httpx import AsyncClient  # Recommended for reuse and performance
from fastapi import FastAPI, HTTPException
# Needed for accessing the client instance
from starlette.requests import Request
import os
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
# Set the base URL for your deployed Node.js Mediasoup Server (e.g., on GCP VM)
# If using a local dev setup, this would be 'http://localhost:3000/api/media'
MEDIA_SERVER_URL = os.getenv("MEDIA_SERVER_URL")


# --- 1. Client Management (Recommended for Production) ---

# We use FastAPI's lifespan events to create and close a single httpx.AsyncClient
# This client instance is stored in the FastAPI application state.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize the client when the app starts
    app.requests_client = httpx.AsyncClient(base_url=MEDIA_SERVER_URL)
    yield
    # Close the client when the app shuts down
    await app.requests_client.aclose()


# --- 2. The Core Proxy Function ---
async def call_media_server(
    request: Request,
    method: str,
    endpoint: str,
    json_data: dict = None
) -> dict:
    """
    Proxies an async request from FastAPI to the Node.js Mediasoup API server.
    """
    client: AsyncClient = request.app.requests_client

    # 1. Determine the correct httpx method based on the input
    if method.upper() == 'GET':
        response = await client.get(
            url=endpoint,  # e.g., /router_capabilities
            timeout=5.0
        )
    elif method.upper() == 'POST':
        response = await client.post(
            url=endpoint,  # e.g., /create_transport
            json=json_data,  # httpx automatically serializes this to JSON
            timeout=5.0
        )
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    # 2. Handle HTTP Errors (Crucial for debugging)
    # response.raise_for_status() will raise an exception for 4xx or 5xx status codes
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(
            f"Error calling Mediasoup Server: {e.response.status_code} {e.response.text}")
        # Raise an internal FastAPI exception to propagate the error
        raise HTTPException(
            status_code=503,
            detail=f"Mediasoup Server Error: Could not execute {endpoint}. Check media server logs."
        ) from e

    # 3. Return the JSON response body
    if response.status_code == 204:
        return {}
    return response.json()
