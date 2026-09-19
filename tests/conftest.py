"""Shared fixtures for the backend test suite.

The app reads its configuration from the environment at import time, so the
environment is prepared *before* ``app.app`` is imported anywhere.

Every test gets a fresh in-memory SQLite database: the ``client`` fixture enters
the app lifespan (Tortoise init + schema generation) and tears it down again.
Tests drive the app through its public HTTP / WebSocket API wherever an endpoint
exists, so they double as contract tests for the frontend.
"""
import os
import tempfile
from itertools import count

_tmp = tempfile.mkdtemp(prefix="ping-server-tests-")
os.environ["DB_CONNECTION_STRING"] = "sqlite://:memory:"
os.environ["DB_GENERATE_SCHEMAS"] = "true"
os.environ["DEBUG"] = "true"  # non-secure cookies so they work over http://testserver
os.environ["MEDIA_ROOT"] = os.path.join(_tmp, "media")
os.environ["LOG_DIR"] = os.path.join(_tmp, "logs")
# ZET-59: Vercel branch previews are matched by regex, not enumerated. Mirror a
# realistic prod value so the CORS + /ws origin tests exercise the real pattern.
os.environ["ALLOWED_ORIGIN_REGEX"] = (
    r"^https://ping-frontend-[a-z0-9-]+-vargakings-projects\.vercel\.app$"
)
# Keep rate limits out of the way for the general suite; the rate-limit test
# lowers them for itself (see test_auth). Limits are read from env per request.
os.environ.setdefault("AUTH_RATE_LIMIT", "10000/minute")
os.environ.setdefault("INVITE_USE_RATE_LIMIT", "10000/minute")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.app import app  # noqa: E402

ORIGIN = "http://localhost:5173"
# A Vercel branch-preview origin that matches ALLOWED_ORIGIN_REGEX above.
PREVIEW_ORIGIN = "https://ping-frontend-abc123-vargakings-projects.vercel.app"
PASSWORD = "correct horse battery staple"

_usernames = count(1)


@pytest.fixture
def app_lifespan():
    """Start the app once for the test; yields a factory for independent clients
    (each client has its own cookie jar, i.e. is its own browser)."""
    with TestClient(app) as primary:
        clients = [primary]

        def make_client() -> TestClient:
            # Reuse the running app/lifespan, but with a separate cookie jar.
            c = TestClient(app)
            c.portal = primary.portal  # share the event loop the DB lives on
            clients.append(c)
            return c

        yield primary, make_client


@pytest.fixture
def client(app_lifespan) -> TestClient:
    primary, _ = app_lifespan
    return primary


@pytest.fixture
def new_client(app_lifespan):
    _, make_client = app_lifespan
    return make_client


def register(client: TestClient, username: str | None = None) -> dict:
    """Register a user on *client* (leaves it logged in) and return /auth/me."""
    username = username or f"user{next(_usernames)}"
    res = client.post("/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 201, res.text
    me = client.get("/auth/me")
    assert me.status_code == 200, me.text
    return me.json()


def create_server(client: TestClient, name: str = "Test server") -> dict:
    res = client.post("/servers/", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


def create_channel(client: TestClient, server_id: int, name: str = "general",
                   channel_type: str = "text") -> dict:
    res = client.post(
        f"/channels/{server_id}/create",
        params={"channel_name": name, "channel_type": channel_type},
    )
    assert res.status_code == 201, res.text
    return res.json()
