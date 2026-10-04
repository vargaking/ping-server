"""Voice moderation: server mute, disconnect and the permission bits behind them."""
import base64
import json

import pytest

from app.models.Role import Role
from app.permissions import ADMIN_PERMISSIONS, MEMBER_PERMISSIONS, Permission
from app.routers import voice
from app.services.permissions import permissions
from app.services.voice_presence import VoiceParticipant, VoicePresence
from tests.conftest import create_channel, create_server, register
from tests.test_permissions import join, promote, run


class FakeSource:
    def __init__(self, channels=None):
        self.channels = channels or {}
        self.removed = []
        self.updated = []

    async def fetch(self):
        return {}

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id, ())

    async def remove_participant(self, channel_id, user_id):
        self.removed.append((channel_id, user_id))

    async def update_participant(self, channel_id, user_id, sources, attributes):
        self.updated.append((channel_id, user_id, sources, attributes))

    async def aclose(self):
        pass


def _in_voice(user_id):
    return (VoiceParticipant(user_id, muted=False, deafened=False),)


@pytest.fixture
def livekit_env(monkeypatch):
    monkeypatch.setattr(voice, "LIVEKIT_URL", "wss://livekit.test")
    monkeypatch.setattr(voice, "LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr(voice, "LIVEKIT_API_SECRET", "test-secret-" + "x" * 32)


@pytest.fixture
def team(client, new_client):
    """Owner (the `client`), two admins, a plain member and a second member."""
    owner = register(client)
    server = create_server(client)
    lounge = create_channel(client, server["id"], "Lounge", "voice")
    games = create_channel(client, server["id"], "Games", "voice")
    admin_client, admin = join(client, new_client, server["id"])
    admin2_client, admin2 = join(client, new_client, server["id"])
    member_client, member = join(client, new_client, server["id"])
    other_client, other = join(client, new_client, server["id"])
    promote(client, server["id"], admin["id"])
    promote(client, server["id"], admin2["id"])
    return {
        "owner": owner, "server": server, "lounge": lounge, "games": games,
        "admin_client": admin_client, "admin": admin, "admin2": admin2,
        "member_client": member_client, "member": member,
        "other_client": other_client, "other": other,
    }


def use_presence(client, monkeypatch, snapshot):
    source = FakeSource(snapshot)

    async def notify(channel_id, participants):
        pass

    presence = VoicePresence(source, notify)
    monkeypatch.setattr(client.app.state, "voice_presence", presence)
    client.portal.call(presence.apply_snapshot, snapshot)
    return source


def mute_url(team, user):
    return f"/api/voice/servers/{team['server']['id']}/members/{user['id']}/server-mute"


def disconnect_url(team, user):
    return f"/api/voice/servers/{team['server']['id']}/members/{user['id']}/disconnect"


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_admin_permissions_include_the_moderation_bits():
    assert Permission.MUTE_MEMBERS == 1 << 12
    assert Permission.MOVE_MEMBERS == 1 << 13
    assert ADMIN_PERMISSIONS & Permission.MUTE_MEMBERS
    assert ADMIN_PERMISSIONS & Permission.MOVE_MEMBERS
    assert not MEMBER_PERMISSIONS & (Permission.MUTE_MEMBERS | Permission.MOVE_MEMBERS)


def test_new_servers_admin_role_has_the_moderation_bits(team, client):
    admin_role = next(
        r for r in client.get(f"/servers/{team['server']['id']}/roles").json()
        if r["name"] == "Admin")
    assert int(admin_role["allow"]) & (Permission.MUTE_MEMBERS | Permission.MOVE_MEMBERS)


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_admin_can_moderate_a_member(team, client, method, url):
    res = getattr(team["admin_client"], method)(url(team, team["member"]))
    assert res.status_code == 204, res.text


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_plain_member_cannot_moderate(team, method, url):
    res = getattr(team["member_client"], method)(url(team, team["other"]))
    assert res.status_code == 403


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_nobody_can_moderate_the_owner(team, method, url):
    assert getattr(team["admin_client"], method)(url(team, team["owner"])).status_code == 403


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_only_the_owner_can_moderate_another_admin(team, client, method, url):
    assert getattr(team["admin_client"], method)(url(team, team["admin2"])).status_code == 403
    assert getattr(client, method)(url(team, team["admin2"])).status_code == 204


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_moderating_yourself_is_rejected(team, client, method, url):
    assert getattr(team["admin_client"], method)(url(team, team["admin"])).status_code == 400
    assert getattr(client, method)(url(team, team["owner"])).status_code == 400


@pytest.mark.parametrize("method,url", [
    ("put", mute_url), ("delete", mute_url), ("post", disconnect_url)])
def test_moderating_a_non_member_is_404(team, new_client, method, url):
    outsider = register(new_client())
    assert getattr(team["admin_client"], method)(url(team, outsider)).status_code == 404


def test_moderating_hides_the_server_from_outsiders(team, new_client):
    outsider = new_client()
    register(outsider)
    assert outsider.put(mute_url(team, team["member"])).status_code == 404
    assert team["admin_client"].put(
        f"/api/voice/servers/999999/members/{team['member']['id']}/server-mute"
    ).status_code == 404


def test_mute_records_state_without_voice_presence(team, client):
    moderation = client.app.state.voice_moderation
    assert client.app.state.voice_presence is None

    for _ in range(2):
        assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    assert moderation.is_muted(team["server"]["id"], team["member"]["id"])

    assert team["admin_client"].delete(mute_url(team, team["member"])).status_code == 204
    assert not moderation.is_muted(team["server"]["id"], team["member"]["id"])


def test_disconnect_without_voice_presence_is_a_no_op(team):
    assert team["admin_client"].post(disconnect_url(team, team["member"])).status_code == 204


def test_mute_takes_the_microphone_in_this_servers_channels_only(team, client, monkeypatch):
    other_server = create_server(client, "Elsewhere")
    elsewhere = create_channel(client, other_server["id"], "Elsewhere", "voice")
    member_id = team["member"]["id"]
    source = use_presence(client, monkeypatch, {
        team["lounge"]["id"]: _in_voice(member_id),
        elsewhere["id"]: _in_voice(member_id),
        team["games"]["id"]: _in_voice(team["other"]["id"]),
    })

    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    assert source.updated == [(
        team["lounge"]["id"], member_id,
        ["screen_share", "screen_share_audio"], {"server_muted": "true"})]


def test_mute_without_stream_leaves_no_sources(team, client, monkeypatch):
    member_id = team["member"]["id"]
    sid = team["server"]["id"]

    async def drop_stream():
        await Role.filter(server_id=sid, is_default=True).update(
            allow=int(MEMBER_PERMISSIONS & ~Permission.STREAM))
        permissions.invalidate(sid)

    run(client, drop_stream)
    source = use_presence(client, monkeypatch, {team["lounge"]["id"]: _in_voice(member_id)})

    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    assert source.updated == [(team["lounge"]["id"], member_id, [], {"server_muted": "true"})]


def test_unmute_restores_sources_from_effective_permissions(team, client, monkeypatch):
    member_id = team["member"]["id"]
    source = use_presence(client, monkeypatch, {team["lounge"]["id"]: _in_voice(member_id)})

    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    assert team["admin_client"].delete(mute_url(team, team["member"])).status_code == 204
    assert source.updated[-1] == (
        team["lounge"]["id"], member_id,
        ["microphone", "screen_share", "screen_share_audio"], {"server_muted": ""})


def test_token_for_a_muted_member_has_no_microphone(team, client, livekit_env):
    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204

    res = team["member_client"].post("/api/voice/token", json={"channel_id": team["lounge"]["id"]})
    assert res.status_code == 200, res.text
    claims = _claims(res.json()["token"])
    assert claims["video"]["canPublishSources"] == ["screen_share", "screen_share_audio"]
    assert claims["attributes"] == {"server_muted": "true"}


def test_token_for_an_unmuted_member_is_unchanged(team, client, livekit_env):
    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    assert team["admin_client"].delete(mute_url(team, team["member"])).status_code == 204

    for who in ("member_client", "other_client"):
        res = team[who].post("/api/voice/token", json={"channel_id": team["lounge"]["id"]})
        claims = _claims(res.json()["token"])
        assert claims["video"]["canPublishSources"] == [
            "microphone", "screen_share", "screen_share_audio"]
        assert "attributes" not in claims or not claims["attributes"]


def test_mute_is_per_server(team, client, new_client, livekit_env):
    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204
    other_server = create_server(team["member_client"], "Mine")
    channel = create_channel(team["member_client"], other_server["id"], "Talk", "voice")

    res = team["member_client"].post("/api/voice/token", json={"channel_id": channel["id"]})
    assert "microphone" in _claims(res.json()["token"])["video"]["canPublishSources"]


def test_disconnect_removes_the_member_from_this_servers_channels_only(team, client, monkeypatch):
    other_server = create_server(client, "Elsewhere")
    elsewhere = create_channel(client, other_server["id"], "Elsewhere", "voice")
    member_id = team["member"]["id"]
    source = use_presence(client, monkeypatch, {
        team["lounge"]["id"]: _in_voice(member_id),
        elsewhere["id"]: _in_voice(member_id),
        team["games"]["id"]: _in_voice(team["other"]["id"]),
    })

    assert team["admin_client"].post(disconnect_url(team, team["member"])).status_code == 204
    assert source.removed == [(team["lounge"]["id"], member_id)]


def test_kicked_member_is_no_longer_server_muted(team, client):
    moderation = client.app.state.voice_moderation
    sid, member_id = team["server"]["id"], team["member"]["id"]
    assert team["admin_client"].put(mute_url(team, team["member"])).status_code == 204

    assert client.delete(f"/servers/{sid}/members/{member_id}").status_code == 204
    assert not moderation.is_muted(sid, member_id)


def test_presence_endpoint_reports_server_muted(team, client, monkeypatch):
    muted = VoiceParticipant(team["member"]["id"], muted=True, deafened=False, server_muted=True)
    use_presence(client, monkeypatch, {team["lounge"]["id"]: (muted,)})

    res = team["other_client"].get(f"/api/voice/presence/{team['server']['id']}")
    assert res.json() == [{
        "channel_id": team["lounge"]["id"],
        "participants": [{
            "user_id": team["member"]["id"],
            "muted": True, "deafened": False, "server_muted": True}],
    }]



def test_migration_grants_the_bits_to_roles_that_can_kick(team, client):
    sid = team["server"]["id"]

    async def run_migration():
        from tortoise import Tortoise

        legacy = int(ADMIN_PERMISSIONS & ~Permission.MUTE_MEMBERS & ~Permission.MOVE_MEMBERS)
        await Role.filter(server_id=sid, name="Admin").update(allow=legacy)
        conn = Tortoise.get_connection("default")
        sql = await __import__(
            "migrations.models.34_20261003180000_voice_moderation_permissions",
            fromlist=["upgrade"]).upgrade(conn)
        await conn.execute_script(sql)
        return {r.name: r.allow for r in await Role.filter(server_id=sid)}

    allow = run(client, run_migration)
    assert allow["Admin"] == int(ADMIN_PERMISSIONS)
    assert allow["@everyone"] == int(MEMBER_PERMISSIONS)
