"""Web Push: the config and subscription endpoints, and when the server pushes
(DMs to peers and @mentions of members with no active session, read
retractions).

``push.deliver`` is the only code that touches the network; the tests replace
it with a recorder, except the ones that point the real one at a local server.
"""
import base64
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from app.models.Message import Message
from app.models.PushSubscription import PushSubscription
from app.models.Role import Role
from app.models.RoleToUser import RoleToUser
from app.permissions import Permission
from app.services.connection_manager import IDLE_AFTER_SECONDS, ConnectionManager
from app.services.permissions import permissions
from app.services.push import (
    DeliveryResult, dm_payload, endpoint_host, is_valid_subject, mention_payload,
    mentioned_user_ids, normalize_subject, plain_text, push, read_payload)
from app.services.push import test_payload as build_test_payload
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}
FCM = "https://fcm.googleapis.com/fcm/send/"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


P256DH = b64url(b"\x04" + bytes(range(64)))
AUTH = b64url(bytes(range(16)))


def new_vapid_env() -> dict[str, str]:
    vapid = Vapid()
    vapid.generate_keys()
    numbers = vapid.private_key.private_numbers()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {
        "VAPID_PUBLIC_KEY": b64url(public),
        "VAPID_PRIVATE_KEY": b64url(numbers.private_value.to_bytes(32, "big")),
        "VAPID_SUBJECT": "mailto:admin@example.com",
    }


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []
        self.statuses: dict[str, int] = {}
        self.bodies: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}

    async def deliver(self, sub, payload, *, topic, urgency):
        self.calls.append({
            "endpoint": sub.endpoint, "user_id": sub.user_id, "payload": payload,
            "topic": topic, "urgency": urgency})
        if sub.endpoint in self.errors:
            raise self.errors[sub.endpoint]
        return DeliveryResult(
            self.statuses.get(sub.endpoint, 201), self.bodies.get(sub.endpoint, ""))


@pytest.fixture
def vapid_env():
    return new_vapid_env()


@pytest.fixture
def push_on(monkeypatch, vapid_env):
    for key, value in vapid_env.items():
        monkeypatch.setenv(key, value)
    push.configure()
    recorder = Recorder()
    monkeypatch.setattr(push, "deliver", recorder.deliver)
    push._pushed.clear()
    yield recorder
    monkeypatch.undo()
    push.configure()
    push._pushed.clear()


@pytest.fixture
def push_off(monkeypatch):
    for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(key, raising=False)
    push.configure()
    recorder = Recorder()
    monkeypatch.setattr(push, "deliver", recorder.deliver)
    push._pushed.clear()
    yield recorder
    monkeypatch.undo()
    push.configure()


def subscribe(client, endpoint=None, **overrides):
    if endpoint is None:
        endpoint = f"{FCM}{uuid.uuid4().hex}"
    body = {
        "endpoint": endpoint,
        "keys": {"p256dh": P256DH, "auth": AUTH},
        **overrides,
    }
    return client.post("/api/push/subscriptions", json=body), endpoint


def run(client, awaitable):
    """Await a query on the app's event loop, where the DB lives."""
    async def wait():
        return await awaitable
    return client.portal.call(wait)


def drain(client):
    client.portal.call(push.drain)


def sync(ws):
    """Return once the server has handled every frame this socket sent so far."""
    ws.send_json({"type": "connection_init"})
    while ws.receive_json()["type"] != "presence_init":
        pass


def report(ws, state):
    ws.send_json({"type": "activity", "state": state})
    sync(ws)


def doc(*nodes):
    return {"type": "doc", "content": [{"type": "paragraph", "content": list(nodes)}]}


def text(value):
    return {"type": "text", "text": value}


def mention(user_id, label="x"):
    return {"type": "mention", "attrs": {"id": user_id, "label": label}}


def dm_frame(conversation_id, content):
    return {
        "type": "direct_message",
        "id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def channel_frame(server_id, channel_id, content):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def open_conversation(client, other_user_id):
    res = client.post("/conversations/", json={"user_id": other_user_id})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture
def dm_pair(client, new_client):
    """alice (the `client`) and bob, with a DM between them and bob subscribed."""
    alice = register(client, "alice-push")
    bob_client = new_client()
    bob = register(bob_client, "bob-push")
    convo = open_conversation(client, bob["id"])
    res, endpoint = subscribe(bob_client)
    assert res.status_code == 201
    return SimpleNamespace(
        alice_client=client, alice=alice, bob_client=bob_client, bob=bob,
        convo=convo, bob_endpoint=endpoint)


@pytest.fixture
def channel_team(client, new_client):
    """alice (owner), bob and carol as members of one server, both subscribed."""
    alice = register(client, "alice-mention")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    members = {}
    for name in ("bob-mention", "carol-mention"):
        member_client = new_client()
        user = register(member_client, name)
        invite_and_join(client, member_client, server["id"])
        res, endpoint = subscribe(member_client)
        assert res.status_code == 201
        members[name.split("-")[0]] = SimpleNamespace(
            client=member_client, user=user, endpoint=endpoint)
    return SimpleNamespace(
        alice_client=client, alice=alice, server=server, channel=channel, **members)


# -- config ----------------------------------------------------------------

def test_config_requires_login(client):
    assert client.get("/api/push/config").status_code == 401


def test_config_is_disabled_without_vapid_env(client, push_off):
    register(client)
    assert client.get("/api/push/config").json() == {"enabled": False, "public_key": None}


@pytest.mark.parametrize("missing", ["VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"])
def test_config_is_disabled_when_any_vapid_var_is_missing(client, push_on, monkeypatch, missing):
    monkeypatch.delenv(missing)
    push.configure()
    register(client)
    assert client.get("/api/push/config").json() == {"enabled": False, "public_key": None}


def test_config_returns_the_public_key(client, push_on, vapid_env):
    register(client)
    assert client.get("/api/push/config").json() == {
        "enabled": True, "public_key": vapid_env["VAPID_PUBLIC_KEY"]}


def test_config_stays_disabled_for_mismatched_or_broken_keys(client, push_on, monkeypatch, vapid_env):
    register(client)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", new_vapid_env()["VAPID_PUBLIC_KEY"])
    push.configure()
    assert client.get("/api/push/config").json()["enabled"] is False

    monkeypatch.setenv("VAPID_PUBLIC_KEY", vapid_env["VAPID_PUBLIC_KEY"])
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "not-a-key")
    push.configure()
    assert client.get("/api/push/config").json()["enabled"] is False

    monkeypatch.setenv("VAPID_PRIVATE_KEY", vapid_env["VAPID_PRIVATE_KEY"])
    monkeypatch.setenv("VAPID_SUBJECT", "admin@example.com")
    push.configure()
    assert client.get("/api/push/config").json()["enabled"] is False


def test_a_pem_private_key_is_accepted(client, monkeypatch, push_on, vapid_env):
    key = Vapid.from_string(vapid_env["VAPID_PRIVATE_KEY"])
    monkeypatch.setenv("VAPID_PRIVATE_KEY", key.private_pem().decode())
    push.configure()
    assert push.enabled
    monkeypatch.setenv("VAPID_PRIVATE_KEY", key.private_pem().decode().strip().replace("\n", "\\n"))
    push.configure()
    assert push.enabled


def test_normalize_subject_strips_everything_but_the_host():
    assert normalize_subject("https://x.app/") == "https://x.app"
    assert normalize_subject("  https://x.app/some/path?q=1#f ") == "https://x.app"
    assert normalize_subject("HTTPS://user@x.app:8443/") == "https://x.app"
    assert normalize_subject("mailto:me@x.app") == "mailto:me@x.app"
    assert normalize_subject("hello") == "hello"


@pytest.mark.parametrize("value", ["mailto:me@example.com", "https://x.app"])
def test_valid_subjects(value):
    assert is_valid_subject(value)


@pytest.mark.parametrize("value", ["hello", "http://x.app", "mailto:nobody", "", "https://x.app/"])
def test_invalid_subjects(value):
    assert not is_valid_subject(value)


@pytest.mark.parametrize("subject", ["https://x.app/", "https://x.app/some/path"])
def test_a_url_subject_is_normalized_and_signs_a_real_push(
        client, monkeypatch, vapid_env, push_endpoint, subject):
    for key, value in vapid_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("VAPID_SUBJECT", subject)
    push.configure()
    handler, endpoint = push_endpoint
    try:
        assert push.enabled
        assert push.subject == "https://x.app"
        result = client.portal.call(
            lambda: push.deliver(real_sub(endpoint), {"v": 1}, topic="dm-9", urgency="high"))
    finally:
        monkeypatch.undo()
        push.configure()

    assert result.status == 201
    assert jwt_claims(handler.seen[0][0]["authorization"])["sub"] == "https://x.app"


def test_the_subject_defaults_to_the_first_https_origin(push_on, monkeypatch, caplog):
    monkeypatch.delenv("VAPID_SUBJECT")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:5173,https://x.app/")
    with caplog.at_level(logging.INFO, logger="app.services.push"):
        push.configure()
    assert push.enabled
    assert push.subject == "https://x.app"
    assert caplog.text.count("https://x.app") == 1


def test_a_mailto_subject_is_kept(push_on, monkeypatch):
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:me@example.com")
    push.configure()
    assert push.enabled
    assert push.subject == "mailto:me@example.com"


def assert_disabled_by_subject(client, caplog):
    assert not push.enabled
    assert push.public_key is None
    assert [r for r in caplog.records if r.levelno == logging.ERROR and "VAPID_SUBJECT" in r.message]
    register(client)
    assert client.get("/api/push/config").json() == {"enabled": False, "public_key": None}
    res = post_test(client)
    assert res.status_code == 409
    assert res.json()["detail"] == "push not configured: invalid VAPID subject"


def test_no_subject_and_no_https_origin_disables_push(client, push_on, monkeypatch, caplog):
    monkeypatch.delenv("VAPID_SUBJECT")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:5173")
    push.configure()
    assert_disabled_by_subject(client, caplog)


@pytest.mark.parametrize("subject", ["hello", "http://x.app", "mailto:nobody"])
def test_a_garbage_subject_disables_push(client, push_on, monkeypatch, caplog, subject):
    monkeypatch.setenv("VAPID_SUBJECT", subject)
    push.configure()
    assert_disabled_by_subject(client, caplog)


def test_no_keys_disables_push_without_an_error(push_off, caplog):
    push.configure()
    assert not push.enabled
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# -- subscribe -------------------------------------------------------------

def test_subscribing_requires_login(client):
    res, _ = subscribe(client)
    assert res.status_code == 401


@pytest.mark.parametrize("host", [
    "fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
    "wns2-par02p.notify.windows.com", "web.push.apple.com", "api.push.apple.com",
])
def test_allowlisted_hosts_are_accepted(client, host):
    register(client)
    res, _ = subscribe(client, f"https://{host}/send/abc")
    assert res.status_code == 201, res.text
    assert isinstance(res.json()["id"], int)


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abc",
    "https://example.com/push",
    "https://localhost/push",
    "https://127.0.0.1/push",
    "https://evilfcm.googleapis.com.evil.com/push",
    "https://notfcm.googleapis.com.example.com/x",
    "https://fcm.googleapis.com@evil.com/push",
    "https://user:pw@fcm.googleapis.com/push",
    "https://xfcm.googleapis.com/push",
    "ftp://fcm.googleapis.com/push",
    "fcm.googleapis.com/push",
    "",
    FCM + "a" * 2100,
])
def test_bad_endpoints_are_rejected(client, endpoint):
    register(client)
    res, _ = subscribe(client, endpoint)
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("keys", [
    {"p256dh": b64url(bytes(64)), "auth": AUTH},
    {"p256dh": b64url(bytes(66)), "auth": AUTH},
    {"p256dh": P256DH, "auth": b64url(bytes(15))},
    {"p256dh": P256DH, "auth": b64url(bytes(17))},
    {"p256dh": "not base64!", "auth": AUTH},
    {"p256dh": P256DH, "auth": "+/+/+/+/+/+/+/+/+/+/+/"},
    {"p256dh": P256DH},
    {},
])
def test_bad_keys_are_rejected(client, keys):
    register(client)
    res, _ = subscribe(client, keys=keys)
    assert res.status_code == 422, res.text


def test_missing_body_fields_are_rejected(client):
    register(client)
    assert client.post("/api/push/subscriptions", json={}).status_code == 422
    assert client.post("/api/push/subscriptions", json={"keys": {"p256dh": P256DH, "auth": AUTH}}).status_code == 422


def test_padded_keys_are_accepted_and_stored_unpadded(client):
    register(client)
    padded = base64.urlsafe_b64encode(bytes(16)).decode()
    res, endpoint = subscribe(client, keys={"p256dh": P256DH + "=", "auth": padded})
    assert res.status_code == 201, res.text
    row = run(client, PushSubscription.get(endpoint=endpoint))
    assert (row.p256dh, row.auth) == (P256DH, padded.rstrip("="))


def test_extra_hosts_extend_the_allowlist(client, monkeypatch):
    register(client)
    assert subscribe(client, "https://push.internal.test/x")[0].status_code == 422
    monkeypatch.setenv("PUSH_EXTRA_HOSTS", "push.internal.test, other.test")
    assert subscribe(client, "https://push.internal.test/x")[0].status_code == 201
    assert subscribe(client, "https://a.other.test/x")[0].status_code == 201


def test_subscribing_twice_is_idempotent(client):
    register(client)
    first, endpoint = subscribe(client)
    second, _ = subscribe(client, endpoint)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    third, _ = subscribe(client, endpoint)
    assert third.status_code == 200
    assert run(client, PushSubscription.filter(endpoint=endpoint).count()) == 1


def test_resubscribing_refreshes_the_keys(client):
    register(client)
    _, endpoint = subscribe(client)
    new_auth = b64url(bytes([9] * 16))
    res, _ = subscribe(client, endpoint, keys={"p256dh": P256DH, "auth": new_auth})
    assert res.status_code == 200
    assert run(client, PushSubscription.get(endpoint=endpoint)).auth == new_auth


def test_the_same_endpoint_from_another_user_is_reassigned(client, new_client):
    alice = register(client, "alice-share")
    bob_client = new_client()
    bob = register(bob_client, "bob-share")
    res, endpoint = subscribe(client)
    assert res.status_code == 201

    moved, _ = subscribe(bob_client, endpoint)
    assert moved.status_code == 200
    assert moved.json() == res.json()
    rows = run(client, PushSubscription.filter(endpoint=endpoint).values_list("user_id", flat=True))
    assert rows == [bob["id"]] and alice["id"] != bob["id"]


def test_each_user_is_capped_at_ten_subscriptions(client, new_client):
    user = register(client, "alice-cap")
    endpoints = []
    for _ in range(10):
        res, endpoint = subscribe(client)
        assert res.status_code == 201
        endpoints.append(endpoint)

    # The second one was used most recently, the third has never been used.
    run(client, PushSubscription.filter(endpoint=endpoints[1]).update(
        last_used_at=datetime.now(timezone.utc)))
    run(client, PushSubscription.filter(endpoint=endpoints[0]).update(
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))

    res, newest = subscribe(client)
    assert res.status_code == 201
    kept = set(run(client, PushSubscription.filter(user_id=user["id"]).values_list("endpoint", flat=True)))
    assert len(kept) == 10
    assert endpoints[0] not in kept
    assert endpoints[1] in kept and newest in kept

    # Another user's cap is separate.
    other = new_client()
    register(other, "bob-cap")
    assert subscribe(other)[0].status_code == 201


def test_moving_an_endpoint_to_a_user_at_the_cap_evicts_their_oldest(client, new_client):
    register(client, "alice-cap2")
    bob_client = new_client()
    register(bob_client, "bob-cap2")
    _, first = subscribe(client)
    for _ in range(9):
        subscribe(client)
    _, alices_own = subscribe(bob_client)

    assert subscribe(client, alices_own)[0].status_code == 200
    assert run(client, PushSubscription.filter(endpoint=alices_own).count()) == 1
    assert run(client, PushSubscription.filter(endpoint=first).count()) == 0


# -- unsubscribe -----------------------------------------------------------

def unsubscribe(client, endpoint):
    return client.request("DELETE", "/api/push/subscriptions", json={"endpoint": endpoint})


def test_unsubscribe_requires_login(client):
    assert unsubscribe(client, FCM + "x").status_code == 401


def test_unsubscribe_deletes_only_your_own_row_and_is_idempotent(client, new_client):
    register(client, "alice-unsub")
    bob_client = new_client()
    register(bob_client, "bob-unsub")
    _, alices = subscribe(client)
    _, bobs = subscribe(bob_client)

    assert unsubscribe(client, bobs).status_code == 204
    assert run(client, PushSubscription.filter(endpoint=bobs).count()) == 1

    assert unsubscribe(client, alices).status_code == 204
    assert run(client, PushSubscription.filter(endpoint=alices).count()) == 0
    assert unsubscribe(client, alices).status_code == 204
    assert unsubscribe(client, "https://nowhere.example/none").status_code == 204


def stored_uuid(client) -> str:
    return str(run(client, Message.all().order_by("-id").first()).uuid)


# -- DMs -------------------------------------------------------------------

def test_dm_to_an_offline_peer_pushes_once(dm_pair, push_on):
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hello "), text("bob"))))
        sync(ws)
    drain(dm_pair.alice_client)

    assert len(push_on.calls) == 1
    call = push_on.calls[0]
    assert call["endpoint"] == dm_pair.bob_endpoint
    assert call["user_id"] == dm_pair.bob["id"]
    assert call["topic"] == f"dm-{dm_pair.convo['id']}"
    assert call["urgency"] == "high"
    payload = call["payload"]
    sent_at = payload.pop("sent_at")
    assert datetime.fromisoformat(sent_at).tzinfo is not None
    assert isinstance(payload.pop("message_id"), int)
    assert payload.pop("message_uuid") == stored_uuid(dm_pair.alice_client)
    assert payload == {
        "v": 1,
        "kind": "dm",
        "tag": f"dm-{dm_pair.convo['id']}",
        "title": "alice-push",
        "body": "hello bob",
        "url": f"/app/direct/{dm_pair.convo['id']}/",
        "count": 1,
    }


def test_dm_count_is_the_unread_messages_from_the_sender(dm_pair, push_on):
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        for word in ("one", "two", "three"):
            ws.send_json(dm_frame(dm_pair.convo["id"], doc(text(word))))
        sync(ws)
    drain(dm_pair.alice_client)

    assert [c["payload"]["count"] for c in push_on.calls] == [1, 2, 3]
    assert {c["topic"] for c in push_on.calls} == {f"dm-{dm_pair.convo['id']}"}
    assert push_on.calls[-1]["payload"]["body"] == "three"


def test_dm_count_starts_after_the_peers_read_marker(dm_pair, push_on):
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("one"))))
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("two"))))
        sync(ws)
        drain(dm_pair.alice_client)

        messages = dm_pair.bob_client.get(
            f"/conversations/{dm_pair.convo['id']}/messages").json()["messages"]
        newest = max(messages, key=lambda m: m["timestamp"])["id"]
        read = dm_pair.bob_client.put(
            f"/conversations/{dm_pair.convo['id']}/read", json={"message_id": newest})
        assert read.status_code == 200, read.text
        drain(dm_pair.alice_client)
        push_on.calls.clear()

        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("three"))))
        sync(ws)
    drain(dm_pair.alice_client)

    assert [c["payload"]["count"] for c in push_on.calls] == [1]


def send_dm_to_bob(pair, text_value="hi"):
    with pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(pair.convo["id"], doc(text(text_value))))
        sync(ws)
    drain(pair.alice_client)


def test_dm_to_a_peer_with_a_silent_socket_pushes(dm_pair, push_on):
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        send_dm_to_bob(dm_pair)
    assert len(push_on.calls) == 1


def test_dm_to_an_active_peer_does_not_push(dm_pair, push_on):
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        send_dm_to_bob(dm_pair)
    assert push_on.calls == []


def test_dm_pushes_once_the_peer_has_been_idle_for_five_minutes(dm_pair, push_on, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ConnectionManager, "clock", staticmethod(lambda: now[0]))
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        now[0] += IDLE_AFTER_SECONDS - 1
        send_dm_to_bob(dm_pair, "early")
        assert push_on.calls == []
        now[0] += 2
        send_dm_to_bob(dm_pair, "late")
    assert len(push_on.calls) == 1
    assert push_on.calls[0]["payload"]["body"] == "late"


def test_dm_pushes_after_the_peer_reports_idle(dm_pair, push_on):
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        report(bob_ws, "idle")
        send_dm_to_bob(dm_pair)
    assert len(push_on.calls) == 1


def test_dm_does_not_push_while_any_socket_of_the_peer_is_active(dm_pair, push_on):
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as idle_ws, \
            dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as active_ws:
        ws_ready(idle_ws)
        ws_ready(active_ws)
        report(idle_ws, "idle")
        report(active_ws, "active")
        send_dm_to_bob(dm_pair)
    assert push_on.calls == []


def test_an_idle_socket_still_counts_as_online_for_presence(channel_team):
    team = channel_team
    with team.bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "idle")
        with team.alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
            assert team.bob.user["id"] in ws_ready(alice_ws)["user_ids"]


def test_dm_push_reaches_every_subscription_of_the_peer(dm_pair, push_on):
    _, second = subscribe(dm_pair.bob_client)
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hi"))))
        sync(ws)
    drain(dm_pair.alice_client)
    assert {c["endpoint"] for c in push_on.calls} == {dm_pair.bob_endpoint, second}


def test_a_gone_subscription_is_deleted_and_the_rest_still_get_the_push(dm_pair, push_on):
    _, second = subscribe(dm_pair.bob_client)
    push_on.statuses[dm_pair.bob_endpoint] = 410
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hi"))))
        sync(ws)
    drain(dm_pair.alice_client)

    assert {c["endpoint"] for c in push_on.calls} == {dm_pair.bob_endpoint, second}
    left = run(dm_pair.alice_client, PushSubscription.filter(
        user_id=dm_pair.bob["id"]).values_list("endpoint", flat=True))
    assert left == [second]
    used = run(dm_pair.alice_client, PushSubscription.get(endpoint=second))
    assert used.last_used_at is not None


@pytest.mark.parametrize("status", [404, 410])
def test_404_and_410_delete_the_subscription(dm_pair, push_on, status):
    push_on.statuses[dm_pair.bob_endpoint] = status
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hi"))))
        sync(ws)
    drain(dm_pair.alice_client)
    assert run(dm_pair.alice_client, PushSubscription.all().count()) == 0


def test_other_failures_keep_the_subscription(dm_pair, push_on, monkeypatch):
    push_on.statuses[dm_pair.bob_endpoint] = 503
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hi"))))
        sync(ws)
    drain(dm_pair.alice_client)
    row = run(dm_pair.alice_client, PushSubscription.get(endpoint=dm_pair.bob_endpoint))
    assert row.last_used_at is None

    async def boom(sub, payload, *, topic, urgency):
        raise RuntimeError("network down")

    monkeypatch.setattr(push, "deliver", boom)
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("again"))))
        sync(ws)
    drain(dm_pair.alice_client)
    assert run(dm_pair.alice_client, PushSubscription.all().count()) == 1


def test_a_slow_push_service_does_not_hold_up_the_sender(dm_pair, push_on, monkeypatch):
    release = threading.Event()

    async def slow(sub, payload, *, topic, urgency):
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.01)
        return DeliveryResult(201)

    monkeypatch.setattr(push, "deliver", slow)
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(dm_pair.convo["id"], doc(text("hi"))))
        sync(ws)  # returns while the push is still pending
        assert push._tasks
        release.set()
    drain(dm_pair.alice_client)
    assert not push._tasks


# -- mentions --------------------------------------------------------------

def send_to_channel(team, content):
    with team.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(channel_frame(team.server["id"], team.channel["id"], content))
        sync(ws)
    drain(team.alice_client)


def test_a_mention_pushes_to_the_offline_member_only(channel_team, push_on):
    team = channel_team
    send_to_channel(team, doc(text("hey "), mention(team.bob.user["id"], "bob"), text(" look")))

    assert len(push_on.calls) == 1
    call = push_on.calls[0]
    assert call["endpoint"] == team.bob.endpoint
    assert call["topic"] == f"ch-{team.channel['id']}"
    assert call["urgency"] == "high"
    payload = call["payload"]
    payload.pop("sent_at")
    assert isinstance(payload.pop("message_id"), int)
    assert payload.pop("message_uuid") == stored_uuid(team.alice_client)
    assert payload == {
        "v": 1,
        "kind": "mention",
        "tag": f"ch-{team.channel['id']}",
        "title": "alice-mention in #general",
        "body": "hey @bob look",
        "url": f"/app/server/{team.server['id']}/channel/{team.channel['id']}/",
        "count": 1,
    }


def test_every_mentioned_member_is_pushed(channel_team, push_on):
    team = channel_team
    send_to_channel(team, doc(mention(team.bob.user["id"]), mention(team.carol.user["id"]),
                              mention(team.bob.user["id"])))
    assert sorted(c["endpoint"] for c in push_on.calls) == sorted([team.bob.endpoint, team.carol.endpoint])


def test_a_plain_channel_message_does_not_push(channel_team, push_on):
    send_to_channel(channel_team, doc(text("hello everyone")))
    assert push_on.calls == []


def test_mentions_of_the_sender_or_non_members_do_not_push(channel_team, new_client, push_on):
    team = channel_team
    outsider_client = new_client()
    outsider = register(outsider_client, "dave-mention")
    assert subscribe(outsider_client)[0].status_code == 201

    send_to_channel(team, doc(mention(team.alice["id"]), mention(outsider["id"]), mention(99999)))
    assert push_on.calls == []


def test_an_active_mentioned_member_is_not_pushed(channel_team, push_on):
    team = channel_team
    with team.bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        send_to_channel(team, doc(mention(team.bob.user["id"]), mention(team.carol.user["id"])))
    assert [c["endpoint"] for c in push_on.calls] == [team.carol.endpoint]


def test_an_idle_mentioned_member_is_pushed(channel_team, push_on):
    team = channel_team
    with team.bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        send_to_channel(team, doc(mention(team.bob.user["id"])))
        report(bob_ws, "active")
        report(bob_ws, "idle")
        send_to_channel(team, doc(mention(team.bob.user["id"])))
    assert len(push_on.calls) == 2


def test_a_mentioned_member_who_cannot_view_the_channel_is_not_pushed(channel_team, push_on):
    team = channel_team
    server_id, bob_id = team.server["id"], team.bob.user["id"]

    async def deny_view():
        role = await Role.create(server_id=server_id, name="Blind", deny=int(Permission.VIEW_CHANNEL))
        await RoleToUser.create(role_id=role.id, user_id=bob_id)
        permissions.invalidate(server_id, bob_id)

    team.alice_client.portal.call(deny_view)
    send_to_channel(team, doc(mention(bob_id), mention(team.carol.user["id"])))
    assert [c["endpoint"] for c in push_on.calls] == [team.carol.endpoint]


def test_string_content_with_mentions_is_understood(channel_team, push_on):
    team = channel_team
    send_to_channel(team, json.dumps(doc(text("yo "), mention(str(team.bob.user["id"]), "bob"))))
    assert [c["endpoint"] for c in push_on.calls] == [team.bob.endpoint]
    assert push_on.calls[0]["payload"]["body"] == "yo @bob"


# -- read retraction -------------------------------------------------------

def test_reading_a_pushed_dm_sends_one_read_push(dm_pair, push_on):
    convo_id = dm_pair.convo["id"]
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(convo_id, doc(text("hi"))))
        sync(ws)
    drain(dm_pair.alice_client)
    assert len(push_on.calls) == 1
    push_on.calls.clear()

    newest = dm_pair.bob_client.get(f"/conversations/{convo_id}/messages").json()["messages"][0]["id"]
    for _ in range(2):
        res = dm_pair.bob_client.put(f"/conversations/{convo_id}/read", json={"message_id": newest})
        assert res.status_code == 200
        drain(dm_pair.alice_client)

    assert len(push_on.calls) == 1
    call = push_on.calls[0]
    assert call["payload"] == {"v": 1, "kind": "read", "tag": f"dm-{convo_id}"}
    assert call["topic"] == f"dm-{convo_id}"
    assert call["urgency"] == "normal"
    assert call["endpoint"] == dm_pair.bob_endpoint


def test_reading_before_the_pushed_message_does_not_retract(dm_pair, push_on):
    convo_id = dm_pair.convo["id"]
    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(convo_id, doc(text("one"))))
        sync(ws)
        drain(dm_pair.alice_client)
        first = dm_pair.bob_client.get(f"/conversations/{convo_id}/messages").json()["messages"][0]["id"]
        ws.send_json(dm_frame(convo_id, doc(text("two"))))
        sync(ws)
    drain(dm_pair.alice_client)
    push_on.calls.clear()

    dm_pair.bob_client.put(f"/conversations/{convo_id}/read", json={"message_id": first})
    drain(dm_pair.alice_client)
    assert push_on.calls == []


def test_replying_retracts_the_push_for_the_sender_too(dm_pair, push_on):
    """Alice is pushed about bob's message; her own reply moves her marker."""
    convo_id = dm_pair.convo["id"]
    assert subscribe(dm_pair.alice_client)[0].status_code == 201
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        bob_ws.send_json(dm_frame(convo_id, doc(text("ping"))))
        sync(bob_ws)
    drain(dm_pair.alice_client)
    assert [c["user_id"] for c in push_on.calls] == [dm_pair.alice["id"]]
    push_on.calls.clear()

    with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        alice_ws.send_json(dm_frame(convo_id, doc(text("pong"))))
        sync(alice_ws)
    drain(dm_pair.alice_client)
    to_alice = [c for c in push_on.calls if c["user_id"] == dm_pair.alice["id"]]
    assert [c["payload"]["kind"] for c in to_alice] == ["read"]


def test_reading_a_mentioned_channel_retracts_the_mention(channel_team, push_on):
    team = channel_team
    send_to_channel(team, doc(mention(team.bob.user["id"])))
    assert len(push_on.calls) == 1
    push_on.calls.clear()

    newest = team.bob.client.get(f"/channels/{team.channel['id']}/messages").json()["messages"][0]["id"]
    res = team.bob.client.put(f"/channels/{team.channel['id']}/read", json={"message_id": newest})
    assert res.status_code == 200, res.text
    drain(team.alice_client)

    assert [c["payload"] for c in push_on.calls] == [
        {"v": 1, "kind": "read", "tag": f"ch-{team.channel['id']}"}]
    assert push_on.calls[0]["topic"] == f"ch-{team.channel['id']}"


def test_reading_a_thread_we_never_pushed_sends_nothing(channel_team, push_on):
    team = channel_team
    send_to_channel(team, doc(text("no mention")))
    newest = team.bob.client.get(f"/channels/{team.channel['id']}/messages").json()["messages"][0]["id"]
    team.bob.client.put(f"/channels/{team.channel['id']}/read", json={"message_id": newest})
    drain(team.alice_client)
    assert push_on.calls == []


# -- push disabled ---------------------------------------------------------

def test_nothing_is_sent_when_push_is_disabled(client, new_client, push_off):
    register(client, "alice-off")
    bob_client = new_client()
    bob = register(bob_client, "bob-off")
    convo = open_conversation(client, bob["id"])
    assert subscribe(bob_client)[0].status_code == 201
    server = create_server(client)
    channel = create_channel(client, server["id"])
    invite_and_join(client, bob_client, server["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(dm_frame(convo["id"], doc(text("hi"))))
        ws.send_json(channel_frame(server["id"], channel["id"], doc(mention(bob["id"]))))
        sync(ws)
    drain(client)
    assert push_off.calls == []

    messages = bob_client.get(f"/conversations/{convo['id']}/messages").json()["messages"]
    assert len(messages) == 1
    newest = bob_client.get(f"/channels/{channel['id']}/messages").json()["messages"][0]["id"]
    assert bob_client.put(f"/channels/{channel['id']}/read", json={"message_id": newest}).status_code == 200
    drain(client)
    assert push_off.calls == []


# -- content helpers -------------------------------------------------------

def test_plain_text_flattens_tiptap_documents():
    content = {"type": "doc", "content": [
        {"type": "paragraph", "content": [text("Hello "), mention(4, "bob"), {"type": "hardBreak"}, text("there")]},
        {"type": "paragraph", "content": [text("second   line")]},
    ]}
    assert plain_text(content) == "Hello @bob there second line"
    assert plain_text(json.dumps(content)) == "Hello @bob there second line"
    assert plain_text(str(content)) == "Hello @bob there second line"
    assert plain_text("just  a string") == "just a string"
    assert plain_text(None) == ""
    assert plain_text(doc({"type": "mention", "attrs": {"id": 4}})) == "@4"


def test_plain_text_describes_attachments_when_there_is_no_text():
    image = SimpleNamespace(kind="image")
    other = SimpleNamespace(kind="file")
    assert plain_text("", [image]) == "Sent an image"
    assert plain_text("", [other]) == "Sent a file"
    assert plain_text("", [image, other]) == "Sent 2 attachments"
    assert plain_text(doc(text("caption")), [image]) == "caption"


def test_plain_text_is_cut_at_140_characters():
    long = plain_text(doc(text("x" * 500)))
    assert len(long) == 140 and long.endswith("…")
    exact = "y" * 140
    assert plain_text(exact) == exact


def test_mentioned_user_ids_walks_nested_nodes():
    content = {"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [mention(3), mention("5")]}]}]},
        {"type": "paragraph", "content": [mention(True), mention("abc"), mention(None), text("@7")]},
    ]}
    assert mentioned_user_ids(content) == {3, 5}
    assert mentioned_user_ids(json.dumps(content)) == {3, 5}
    assert mentioned_user_ids("plain") == set()
    assert mentioned_user_ids(None) == set()
    assert mentioned_user_ids({"type": "doc", "content": "oops"}) == set()


# -- payload contract ------------------------------------------------------

KEYS_BY_KIND = {
    "dm": {"v", "kind", "tag", "title", "body", "url", "count",
           "message_id", "message_uuid", "sent_at"},
    "read": {"v", "kind", "tag"},
}
KEYS_BY_KIND["mention"] = KEYS_BY_KIND["dm"]


def assert_service_worker_contract(payload):
    assert payload["v"] == 1 and type(payload["v"]) is int
    assert isinstance(payload["tag"], str)
    assert payload["kind"] in {"dm", "mention", "read"}
    assert set(payload) == KEYS_BY_KIND[payload["kind"]]


def test_payload_builders_satisfy_the_service_worker_contract():
    message_uuid = str(uuid.uuid4())
    dm = dm_payload(conversation_id=4, sender_name="amy", body="hi", count=2,
                    message_id=9, message_uuid=message_uuid)
    mention = mention_payload(server_id=1, channel_id=2, channel_name="general",
                              sender_name="amy", body="hi", message_id=9,
                              message_uuid=message_uuid)
    for payload in (dm, mention, read_payload("dm-4"), build_test_payload()):
        assert_service_worker_contract(payload)
    assert (dm["tag"], dm["title"], dm["url"], dm["count"]) == ("dm-4", "amy", "/app/direct/4/", 2)
    assert mention["tag"] == "ch-2"
    assert mention["title"] == "amy in #general"
    assert mention["url"] == "/app/server/1/channel/2/"
    assert dm["message_uuid"] == message_uuid


def test_a_real_dm_push_carries_the_uuid_the_peer_sees(dm_pair, push_on):
    frame = dm_frame(dm_pair.convo["id"], doc(text("hi")))
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        with dm_pair.alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
            ws_ready(ws)
            ws.send_json(frame)
            sync(ws)
        received = bob_ws.receive_json()
    drain(dm_pair.alice_client)

    payload = push_on.calls[0]["payload"]
    assert_service_worker_contract(payload)
    assert received["type"] == "direct_message"
    assert payload["message_uuid"] == received["id"] == frame["id"]


def test_a_real_mention_push_carries_the_uuid_of_the_stored_message(channel_team, push_on):
    team = channel_team
    send_to_channel(team, doc(mention(team.bob.user["id"])))
    payload = push_on.calls[0]["payload"]
    assert_service_worker_contract(payload)
    assert payload["message_uuid"] == stored_uuid(team.alice_client)


# -- test endpoint ---------------------------------------------------------

def post_test(client):
    return client.post("/api/push/test")


def test_the_test_endpoint_requires_login(client):
    assert post_test(client).status_code == 401


def test_the_test_endpoint_is_a_conflict_when_push_is_disabled(client, push_off):
    register(client, "tester")
    res = post_test(client)
    assert res.status_code == 409
    assert res.json()["detail"] == "push not configured: VAPID keys not set"


def test_the_test_endpoint_returns_an_empty_list_without_subscriptions(client, push_on):
    register(client, "tester")
    res = post_test(client)
    assert res.status_code == 200
    assert res.json() == []
    assert push_on.calls == []


def test_the_test_endpoint_pushes_to_every_subscription_of_the_caller_only(client, new_client, push_on):
    register(client, "tester")
    _, first = subscribe(client)
    _, second = subscribe(client)
    other_client = new_client()
    register(other_client, "someone-else")
    subscribe(other_client)

    res = post_test(client)

    assert res.status_code == 200
    assert res.json() == [
        {"endpoint_host": "fcm.googleapis.com", "status": 201, "error": None}] * 2
    assert {c["endpoint"] for c in push_on.calls} == {first, second}
    for call in push_on.calls:
        assert (call["topic"], call["urgency"]) == ("test", "high")
        assert_service_worker_contract(call["payload"])
        assert call["payload"]["kind"] == "dm"
        assert call["payload"]["tag"] == "test"
        assert call["payload"]["message_id"] == 0


def test_the_test_endpoint_ignores_the_active_session_check(client, push_on):
    register(client, "tester")
    subscribe(client)
    with client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        report(ws, "active")
        res = post_test(client)
    assert len(res.json()) == 1
    assert len(push_on.calls) == 1


def test_the_test_endpoint_reports_failures_per_subscription(client, push_on):
    register(client, "tester")
    _, rejected = subscribe(client)
    _, broken = subscribe(client)
    _, fine = subscribe(client)
    push_on.statuses[rejected] = 403
    push_on.bodies[rejected] = "invalid JWT provided"
    push_on.errors[broken] = ConnectionError("connection refused")

    by_status = {r["status"]: r for r in post_test(client).json()}

    assert by_status[403]["error"] == "invalid JWT provided"
    assert by_status[None]["error"] == "ConnectionError: connection refused"
    assert by_status[201]["error"] is None
    assert run(client, PushSubscription.all().count()) == 3


def test_the_test_endpoint_deletes_a_gone_subscription(client, push_on):
    register(client, "tester")
    _, gone = subscribe(client)
    push_on.statuses[gone] = 410
    res = post_test(client)
    assert res.json()[0]["status"] == 410
    assert run(client, PushSubscription.all().count()) == 0


def test_the_test_endpoint_drops_a_gone_subscription_for_good(client, push_on):
    register(client, "tester")
    _, gone = subscribe(client)
    _, fine = subscribe(client)
    push_on.statuses[gone] = 410

    assert sorted(r["status"] for r in post_test(client).json()) == [201, 410]
    assert run(client, PushSubscription.filter(endpoint=gone).count()) == 0
    assert [r["status"] for r in post_test(client).json()] == [201]


def test_the_test_endpoint_does_not_track_for_read_retraction(client, push_on):
    register(client, "tester")
    subscribe(client)
    post_test(client)
    assert push._pushed == {}


def test_the_test_endpoint_is_rate_limited(client, push_on, monkeypatch):
    monkeypatch.setenv("PUSH_TEST_RATE_LIMIT", "2/minute")
    register(client, "tester")
    assert [post_test(client).status_code for _ in range(3)] == [200, 200, 429]


# -- logging ---------------------------------------------------------------

def test_a_sent_push_logs_user_host_and_status_but_not_the_endpoint(dm_pair, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.push")
    send_dm_to_bob(dm_pair)
    assert f"Push sent to user {dm_pair.bob['id']} via fcm.googleapis.com: 201" in caplog.text
    assert dm_pair.bob_endpoint not in caplog.text
    assert endpoint_host(dm_pair.bob_endpoint) == "fcm.googleapis.com"


def test_a_failed_push_logs_the_response_body(dm_pair, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.push")
    push_on.statuses[dm_pair.bob_endpoint] = 403
    push_on.bodies[dm_pair.bob_endpoint] = "invalid JWT provided"
    send_dm_to_bob(dm_pair)
    assert "failed: 403 invalid JWT provided" in caplog.text
    assert dm_pair.bob_endpoint not in caplog.text


def test_a_raised_delivery_error_is_logged_with_user_and_host(dm_pair, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.push")
    push_on.errors[dm_pair.bob_endpoint] = RuntimeError("network down")
    send_dm_to_bob(dm_pair)
    assert f"Push to user {dm_pair.bob['id']} via fcm.googleapis.com raised" in caplog.text
    assert dm_pair.bob_endpoint not in caplog.text


def test_a_user_without_subscriptions_logs_the_skip(dm_pair, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.push")
    run(dm_pair.alice_client, PushSubscription.all().delete())
    send_dm_to_bob(dm_pair)
    assert f"Push skipped for user {dm_pair.bob['id']}: no subscription" in caplog.text


def test_a_dm_to_an_active_peer_logs_the_skip(dm_pair, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.chat_service")
    with dm_pair.bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        send_dm_to_bob(dm_pair)
    assert f"Push skipped for user {dm_pair.bob['id']}: active session" in caplog.text


def test_an_active_mentioned_member_logs_the_skip(channel_team, push_on, caplog):
    caplog.set_level(logging.INFO, logger="app.services.chat_service")
    team = channel_team
    with team.bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        report(bob_ws, "active")
        send_to_channel(team, doc(mention(team.bob.user["id"])))
    assert f"Push skipped for user {team.bob.user['id']}: active session" in caplog.text


# -- the real deliver ------------------------------------------------------

class _Capture(BaseHTTPRequestHandler):
    status = 201
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(type(self).status)
        if 300 <= type(self).status < 400:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def push_endpoint(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    handler = type("Capture", (_Capture,), {"seen": [], "status": 201})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}/push/abc"
    server.shutdown()
    server.server_close()


def real_sub(endpoint):
    return SimpleNamespace(endpoint=endpoint, p256dh=b64url(_ua_public()), auth=AUTH)


def _ua_public() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def jwt_claims(authorization: str) -> dict:
    token = authorization.removeprefix("vapid t=").split(",")[0]
    segment = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def test_deliver_sends_an_encrypted_vapid_signed_request(client, monkeypatch, vapid_env, push_endpoint):
    for key, value in vapid_env.items():
        monkeypatch.setenv(key, value)
    push.configure()
    handler, endpoint = push_endpoint
    try:
        result = client.portal.call(
            lambda: push.deliver(real_sub(endpoint), {"v": 1}, topic="dm-9", urgency="high"))
    finally:
        monkeypatch.undo()
        push.configure()

    assert result.status == 201
    headers, body = handler.seen[0]
    assert headers["ttl"] == "345600"
    assert headers["topic"] == "dm-9"
    assert headers["urgency"] == "high"
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=")
    assert f"k={vapid_env['VAPID_PUBLIC_KEY']}" in headers["authorization"]
    assert b'"v"' not in body  # encrypted

    claims = jwt_claims(headers["authorization"])
    assert claims["aud"] == endpoint.rsplit("/push/", 1)[0]
    assert claims["sub"] == "mailto:admin@example.com"
    assert abs(claims["exp"] - (time.time() + 12 * 60 * 60)) < 60


def test_deliver_returns_the_error_status_of_the_push_service(client, monkeypatch, vapid_env, push_endpoint):
    for key, value in vapid_env.items():
        monkeypatch.setenv(key, value)
    push.configure()
    handler, endpoint = push_endpoint
    handler.status = 410
    try:
        result = client.portal.call(
            lambda: push.deliver(real_sub(endpoint), {"v": 1}, topic="dm-9", urgency="normal"))
    finally:
        monkeypatch.undo()
        push.configure()
    assert result.status == 410


def test_deliver_does_not_follow_redirects(client, monkeypatch, vapid_env, push_endpoint):
    for key, value in vapid_env.items():
        monkeypatch.setenv(key, value)
    push.configure()
    handler, endpoint = push_endpoint
    handler.status = 307
    try:
        with pytest.raises(Exception):
            client.portal.call(
                lambda: push.deliver(real_sub(endpoint), {"v": 1}, topic="dm-9", urgency="normal"))
    finally:
        monkeypatch.undo()
        push.configure()
    assert len(handler.seen) == 1


# -- hostile content -------------------------------------------------------

def nested_doc(depth, leaf=None):
    node = leaf or text("deep")
    for _ in range(depth):
        node = {"type": "blockquote", "content": [node]}
    return {"type": "doc", "content": [node]}


def test_walkers_are_bounded_on_deep_and_huge_documents():
    deep = nested_doc(5000, mention(4))
    assert mentioned_user_ids(deep) == set()
    assert plain_text(deep) == ""

    wide = {"type": "doc", "content": [text("x")] * 50_000 + [mention(4)]}
    assert mentioned_user_ids(wide) == set()
    assert plain_text(wide).startswith("xxxx")

    assert mentioned_user_ids(doc(mention(1), mention(2))) == {1, 2}


@pytest.mark.parametrize("raw", ["(" * 100_000, "[" * 100_000, "{" * 5_000 + "}" * 5_000, "1" * 100_000])
def test_hostile_strings_fall_back_to_plain_text(raw):
    assert mentioned_user_ids(raw) == set()
    assert plain_text(raw).startswith(raw[:100])


def hostile_message_is_delivered_without_push(team, push_on, send_raw):
    with team.alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            team.bob.client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        send_raw(alice_ws)
        frame = bob_ws.receive_json()
        while frame["type"] != "message":
            frame = bob_ws.receive_json()
        sync(alice_ws)
    drain(team.alice_client)
    assert push_on.calls == []
    return frame


def test_a_100k_paren_string_message_is_delivered_and_sends_no_push(channel_team, push_on):
    team = channel_team
    content = "(" * 100_000
    frame = hostile_message_is_delivered_without_push(
        team, push_on,
        lambda ws: ws.send_json(channel_frame(team.server["id"], team.channel["id"], content)))
    assert frame["content"] == content


def test_a_deeply_nested_document_is_delivered_and_sends_no_push(channel_team, push_on):
    team = channel_team
    body = json.dumps(channel_frame(team.server["id"], team.channel["id"], None))
    # Deeper than the walkers' cap, but within what json can parse and echo.
    depth = 300
    nested = '{"type":"blockquote","content":[' * depth + '{"type":"text","text":"deep"}' + "]}" * depth
    raw = body.replace("null", nested)
    frame = hostile_message_is_delivered_without_push(
        team, push_on, lambda ws: ws.send_text(raw))
    assert frame["channel_id"] == team.channel["id"]


def test_a_failing_mention_scan_does_not_break_delivery(channel_team, push_on, monkeypatch):
    team = channel_team

    def boom(content):
        raise RuntimeError("scan failed")

    monkeypatch.setattr("app.services.chat_service.mentioned_user_ids", boom)
    frame = hostile_message_is_delivered_without_push(
        team, push_on,
        lambda ws: ws.send_json(channel_frame(team.server["id"], team.channel["id"], doc(text("hi")))))
    assert frame["channel_id"] == team.channel["id"]
