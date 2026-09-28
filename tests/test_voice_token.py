"""Voice token minting: membership checks and the grants in the token."""
import base64
import json

import pytest

from app.routers import voice
from tests.conftest import create_channel, create_server, register


@pytest.fixture
def livekit_env(monkeypatch):
    monkeypatch.setattr(voice, "LIVEKIT_URL", "wss://livekit.test")
    monkeypatch.setattr(voice, "LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr(voice, "LIVEKIT_API_SECRET", "test-secret-" + "x" * 32)


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_token_scopes_room_and_allows_own_attributes(client, livekit_env):
    me = register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "Hangout", "voice")

    res = client.post("/api/voice/token", json={"channel_id": channel["id"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"] == "wss://livekit.test"
    assert body["room"] == f"channel_{channel['id']}"

    claims = _claims(body["token"])
    assert claims["sub"] == str(me["id"])
    grant = claims["video"]
    assert grant["room"] == f"channel_{channel['id']}"
    assert grant["roomJoin"] is True
    # Needed to publish deafen state as a participant attribute.
    assert grant["canUpdateOwnMetadata"] is True


def test_token_requires_membership_and_voice_channel(client, new_client, livekit_env):
    register(client)
    server = create_server(client)
    text = create_channel(client, server["id"], "general", "text")
    voice_ch = create_channel(client, server["id"], "Hangout", "voice")

    assert client.post("/api/voice/token", json={"channel_id": text["id"]}).status_code == 400

    outsider = new_client()
    register(outsider)
    assert outsider.post("/api/voice/token",
                         json={"channel_id": voice_ch["id"]}).status_code == 403


def test_token_503_when_livekit_unconfigured(client, monkeypatch):
    monkeypatch.setattr(voice, "LIVEKIT_URL", "")
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "Hangout", "voice")
    assert client.post("/api/voice/token", json={"channel_id": channel["id"]}).status_code == 503
