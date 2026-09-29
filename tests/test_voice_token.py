"""Voice token minting: membership checks and the grants in the token."""
import base64
import json

import pytest

from app.models.Role import Role
from app.permissions import MEMBER_PERMISSIONS, Permission
from app.routers import voice
from app.services.permissions import permissions
from tests.conftest import create_channel, create_server, register
from tests.test_realtime_events import invite_and_join


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


def _member_grant(client, new_client, everyone_mask: Permission):
    """Token grant for a plain member after @everyone is set to *everyone_mask*."""
    register(client)
    server = create_server(client)
    channel = create_channel(client, server["id"], "Hangout", "voice")
    member = new_client()
    register(member)
    invite_and_join(client, member, server["id"])

    async def restrict():
        await Role.filter(server_id=server["id"], is_default=True).update(allow=int(everyone_mask))
        permissions.invalidate(server["id"])

    client.portal.call(restrict)
    res = member.post("/api/voice/token", json={"channel_id": channel["id"]})
    assert res.status_code == 200, res.text
    return _claims(res.json()["token"])["video"]


def test_default_member_may_publish_mic_and_screen(client, new_client, livekit_env):
    grant = _member_grant(client, new_client, MEMBER_PERMISSIONS)
    assert grant["canPublish"] is True
    assert grant["canPublishSources"] == ["microphone", "screen_share", "screen_share_audio"]


def test_member_without_stream_cannot_publish_screen(client, new_client, livekit_env):
    grant = _member_grant(client, new_client, MEMBER_PERMISSIONS & ~Permission.STREAM)
    assert grant["canPublish"] is True
    assert grant["canPublishSources"] == ["microphone"]


def test_member_without_speak_and_stream_cannot_publish(client, new_client, livekit_env):
    grant = _member_grant(
        client, new_client, MEMBER_PERMISSIONS & ~Permission.SPEAK & ~Permission.STREAM)
    assert grant["canPublish"] is False
    assert not grant.get("canPublishSources")
