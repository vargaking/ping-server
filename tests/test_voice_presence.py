"""Voice presence: mapping LiveKit participants, diffing snapshots, the polling
loop, and how state reaches server members over the websocket and REST."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from livekit.protocol import models

from app.services.voice_presence import (
    VoiceParticipant,
    LiveKitRoomSource,
    VoicePresence,
    livekit_api_url,
    make_broadcast_notify,
    participant_from_info,
)
from tests.conftest import ORIGIN, create_channel, create_server, register
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}


def _mic(muted: bool) -> models.TrackInfo:
    return models.TrackInfo(
        type=models.TrackType.AUDIO,
        source=models.TrackSource.MICROPHONE,
        muted=muted,
    )


def _info(identity="7", tracks=(), attributes=None, state=models.ParticipantInfo.State.ACTIVE):
    return models.ParticipantInfo(
        identity=identity, tracks=list(tracks), attributes=attributes or {}, state=state)


class FakeSource:
    def __init__(self, *results, channels=None):
        self._results = list(results)
        self._channels = channels or {}
        self.channel_reads = []
        self.closed = False

    async def fetch(self):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fetch_channel(self, channel_id):
        self.channel_reads.append(channel_id)
        result = self._channels.get(channel_id, ())
        if callable(result):
            result = await result()
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self):
        self.closed = True


class Recorder:
    def __init__(self, fail_for=()):
        self.calls = []
        self._fail_for = set(fail_for)

    async def __call__(self, channel_id, participants):
        if channel_id in self._fail_for:
            raise RuntimeError("boom")
        self.calls.append((channel_id, participants))


def alice(muted=False, deafened=False):
    return VoiceParticipant(user_id=1, muted=muted, deafened=deafened)


def bob(muted=False, deafened=False):
    return VoiceParticipant(user_id=2, muted=muted, deafened=deafened)


@pytest.fixture
def presence_logs():
    """Records app.voice_presence log records. Used instead of caplog so the
    tests also work with the logging plugin disabled."""
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("app.voice_presence")
    handler = Collect()
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


# participant_from_info

def test_unmuted_mic_is_not_muted():
    assert participant_from_info(_info(tracks=[_mic(False)])) == VoiceParticipant(7, False, False)


def test_muted_mic_is_muted():
    assert participant_from_info(_info(tracks=[_mic(True)])).muted is True


def test_no_tracks_counts_as_muted():
    assert participant_from_info(_info()).muted is True


def test_deafened_attribute():
    info = _info(tracks=[_mic(False)], attributes={"deafened": "true"})
    assert participant_from_info(info).deafened is True
    assert participant_from_info(_info(attributes={"deafened": "false"})).deafened is False


def test_non_numeric_identity_is_skipped():
    assert participant_from_info(_info(identity="ingress-bot")) is None


def test_disconnected_participant_is_skipped():
    info = _info(state=models.ParticipantInfo.State.DISCONNECTED)
    assert participant_from_info(info) is None


# LiveKitRoomSource

def test_source_maps_channel_rooms_to_sorted_participants():
    rooms = {
        "channel_5": [_info("9", [_mic(False)]), _info("3", [_mic(True)]),
                      _info("bot"), _info("4", state=models.ParticipantInfo.State.DISCONNECTED)],
        "channel_6": [],
        "channel_x": [_info("1")],
        "other_room": [_info("2")],
    }

    async def list_rooms(_request):
        return SimpleNamespace(rooms=[
            models.Room(name=name, num_participants=len(members))
            for name, members in rooms.items()
        ])

    async def list_participants(request):
        return SimpleNamespace(participants=rooms[request.room])

    source = LiveKitRoomSource("http://livekit.test", "key", "secret")
    source._client = SimpleNamespace(
        room=SimpleNamespace(list_rooms=list_rooms, list_participants=list_participants))

    snapshot = asyncio.run(source.fetch())

    assert snapshot == {5: (VoiceParticipant(3, True, False), VoiceParticipant(9, False, False))}


def test_source_lists_rooms_whose_participant_count_lags():
    async def list_rooms(_request):
        return SimpleNamespace(rooms=[models.Room(name="channel_5", num_participants=0)])

    async def list_participants(request):
        assert request.room == "channel_5"
        return SimpleNamespace(participants=[_info("3", [_mic(False)])])

    source = LiveKitRoomSource("http://livekit.test", "key", "secret")
    source._client = SimpleNamespace(
        room=SimpleNamespace(list_rooms=list_rooms, list_participants=list_participants))

    assert asyncio.run(source.fetch()) == {5: (VoiceParticipant(3, False, False),)}


# VoicePresence diffing

def test_first_snapshot_notifies_each_channel():
    recorder = Recorder()
    presence = VoicePresence(FakeSource(), recorder)

    asyncio.run(presence.apply_snapshot({1: (alice(),), 2: (bob(),)}))

    assert sorted(recorder.calls) == [(1, (alice(),)), (2, (bob(),))]
    assert presence.channel_participants(1) == (alice(),)
    assert presence.channel_participants(99) == ()


def test_unchanged_snapshot_notifies_nothing():
    recorder = Recorder()
    presence = VoicePresence(FakeSource(), recorder)

    async def scenario():
        await presence.apply_snapshot({1: (alice(),)})
        recorder.calls.clear()
        await presence.apply_snapshot({1: (alice(),)})

    asyncio.run(scenario())
    assert recorder.calls == []


def test_mute_change_notifies_only_that_channel():
    recorder = Recorder()
    presence = VoicePresence(FakeSource(), recorder)

    async def scenario():
        await presence.apply_snapshot({1: (alice(),), 2: (bob(),)})
        recorder.calls.clear()
        await presence.apply_snapshot({1: (alice(muted=True),), 2: (bob(),)})

    asyncio.run(scenario())
    assert recorder.calls == [(1, (alice(muted=True),))]


def test_emptied_room_notifies_with_empty_tuple():
    recorder = Recorder()
    presence = VoicePresence(FakeSource(), recorder)

    async def scenario():
        await presence.apply_snapshot({1: (alice(),), 2: (bob(),)})
        recorder.calls.clear()
        await presence.apply_snapshot({2: (bob(),)})

    asyncio.run(scenario())
    assert recorder.calls == [(1, ())]
    assert presence.channel_participants(1) == ()


def test_notify_failure_does_not_stop_other_channels(presence_logs):
    recorder = Recorder(fail_for={1})
    presence = VoicePresence(FakeSource(), recorder)

    asyncio.run(presence.apply_snapshot({1: (alice(),), 2: (bob(),)}))

    assert recorder.calls == [(2, (bob(),))]
    assert any(r.levelno == logging.WARNING and r.exc_info for r in presence_logs)


# VoicePresence.refresh

async def _settle(presence):
    while presence._refreshing:
        await asyncio.gather(*presence._refreshing.values())


def test_refresh_reads_one_channel_and_notifies():
    recorder = Recorder()
    source = FakeSource(channels={1: (alice(),)})
    presence = VoicePresence(source, recorder)

    async def scenario():
        presence.refresh(1)
        await _settle(presence)

    asyncio.run(scenario())
    assert source.channel_reads == [1]
    assert recorder.calls == [(1, (alice(),))]
    assert presence.channel_participants(1) == (alice(),)


def test_refresh_emptying_a_channel_notifies_empty():
    recorder = Recorder()
    presence = VoicePresence(FakeSource(channels={1: ()}), recorder)

    async def scenario():
        await presence.apply_snapshot({1: (alice(),)})
        recorder.calls.clear()
        presence.refresh(1)
        await _settle(presence)

    asyncio.run(scenario())
    assert recorder.calls == [(1, ())]
    assert presence.channel_participants(1) == ()


def test_refreshes_during_a_read_fold_into_one_more_read():
    release = asyncio.Event()
    reads = 0

    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            await release.wait()
            return (alice(),)
        return (alice(muted=True),)

    recorder = Recorder()
    source = FakeSource(channels={1: read})
    presence = VoicePresence(source, recorder)

    async def scenario():
        presence.refresh(1)
        await asyncio.sleep(0)
        for _ in range(5):
            presence.refresh(1)
        release.set()
        await _settle(presence)

    asyncio.run(scenario())
    assert reads == 2
    assert recorder.calls[-1] == (1, (alice(muted=True),))
    assert presence.channel_participants(1) == (alice(muted=True),)


def test_slow_poll_does_not_overwrite_a_newer_refresh():
    """A poll that started before someone joined must not remove them again
    after a refresh has already shown them."""
    poll_started = asyncio.Event()
    finish_poll = asyncio.Event()

    class SlowPollSource(FakeSource):
        async def fetch(self):
            poll_started.set()
            await finish_poll.wait()
            return {}

    recorder = Recorder()
    presence = VoicePresence(SlowPollSource(channels={1: (alice(),)}), recorder)

    async def scenario():
        poll = asyncio.create_task(presence.poll_once())
        await poll_started.wait()
        presence.refresh(1)
        await _settle(presence)
        finish_poll.set()
        await poll

    asyncio.run(scenario())
    assert recorder.calls == [(1, (alice(),))]
    assert presence.channel_participants(1) == (alice(),)


def test_later_poll_still_applies_after_a_refresh():
    recorder = Recorder()
    presence = VoicePresence(FakeSource({}, channels={1: (alice(),)}), recorder)

    async def scenario():
        presence.refresh(1)
        await _settle(presence)
        await presence.poll_once()

    asyncio.run(scenario())
    assert recorder.calls == [(1, (alice(),)), (1, ())]


def test_failed_refresh_keeps_state_and_allows_the_next(presence_logs):
    recorder = Recorder()
    source = FakeSource(channels={1: RuntimeError("livekit down")})
    presence = VoicePresence(source, recorder)

    async def scenario():
        await presence.apply_snapshot({1: (alice(),)})
        presence.refresh(1)
        await _settle(presence)
        presence.refresh(1)
        await _settle(presence)

    asyncio.run(scenario())
    assert source.channel_reads == [1, 1]
    assert presence.channel_participants(1) == (alice(),)
    assert not any(r.levelno >= logging.WARNING for r in presence_logs)


def test_aclose_cancels_pending_refreshes():
    never = asyncio.Event()

    async def hang():
        await never.wait()

    source = FakeSource(channels={1: hang})
    presence = VoicePresence(source, Recorder())

    async def scenario():
        presence.refresh(1)
        await asyncio.sleep(0)
        await presence.aclose()

    asyncio.run(scenario())
    assert presence._refreshing == {}
    assert source.closed


# VoicePresence.run backoff

def _run_until_cancelled(source, stop_after, **kwargs):
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == stop_after:
            raise asyncio.CancelledError

    presence = VoicePresence(source, Recorder(), sleep=sleep, **kwargs)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(presence.run())
    return delays


def test_run_backs_off_then_recovers(presence_logs):
    error = RuntimeError("livekit down")

    delays = _run_until_cancelled(FakeSource(error, error, error, {}, {}), stop_after=5)

    assert delays == [3, 6, 12, 3, 3]
    warnings = [r for r in presence_logs if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert sum("recovered" in r.getMessage() for r in presence_logs) == 1


def test_run_backoff_is_capped():
    error = RuntimeError("livekit down")

    delays = _run_until_cancelled(FakeSource(*[error] * 7), stop_after=7)

    assert delays == [3, 6, 12, 24, 48, 60, 60]


# livekit_api_url

@pytest.fixture
def livekit_url_env(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    return monkeypatch


def test_api_url_prefers_explicit_setting(livekit_url_env):
    livekit_url_env.setenv("LIVEKIT_URL", "wss://lk.example.com")
    livekit_url_env.setenv("LIVEKIT_API_URL", "http://livekit:7880")
    assert livekit_api_url() == "http://livekit:7880"


def test_api_url_derived_from_public_url(livekit_url_env):
    livekit_url_env.setenv("LIVEKIT_URL", "wss://lk.example.com")
    assert livekit_api_url() == "https://lk.example.com"
    livekit_url_env.setenv("LIVEKIT_URL", "ws://localhost:7880")
    assert livekit_api_url() == "http://localhost:7880"


def test_api_url_none_when_unset(livekit_url_env):
    assert livekit_api_url() is None


# Wiring

def test_presence_disabled_without_livekit_config(client):
    me = register(client)
    server = create_server(client)
    assert me["id"]

    assert client.app.state.voice_presence is None
    res = client.get(f"/api/voice/presence/{server['id']}")
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_voice_state_reaches_only_server_members(client, new_client, monkeypatch):
    alice_user = register(client, "alice-vp")
    server = create_server(client)
    voice = create_channel(client, server["id"], "Hangout", "voice")
    bob_client = new_client()
    register(bob_client, "bob-vp")
    invite_and_join(client, bob_client, server["id"])
    carol_client = new_client()
    register(carol_client, "carol-vp")
    create_server(carol_client, "Elsewhere")

    presence = VoicePresence(FakeSource(), make_broadcast_notify(client.app.state.comms))
    monkeypatch.setattr(client.app.state, "voice_presence", presence)
    snapshot = {voice["id"]: (VoiceParticipant(alice_user["id"], muted=True, deafened=False),)}

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        assert bob_ws.receive_json()["type"] == "presence_init"
        assert carol_ws.receive_json()["type"] == "presence_init"

        client.portal.call(presence.apply_snapshot, snapshot)

        assert bob_ws.receive_json() == {
            "type": "voice_state",
            "server_id": server["id"],
            "channel_id": voice["id"],
            "participants": [
                {"user_id": alice_user["id"], "muted": True, "deafened": False},
            ],
        }

        # Frames arrive in order, so a voice_state queued for Carol would come
        # before the reply to this legacy snapshot request.
        carol_ws.send_json({"type": "connection_init"})
        assert carol_ws.receive_json()["type"] == "presence_init"

        # Everyone leaving is announced with an empty list.
        client.portal.call(presence.apply_snapshot, {})
        assert bob_ws.receive_json()["participants"] == []


def test_presence_endpoint_lists_occupied_channels_for_members(client, new_client, monkeypatch):
    alice_user = register(client, "alice-ep")
    server = create_server(client)
    occupied = create_channel(client, server["id"], "Hangout", "voice")
    create_channel(client, server["id"], "Empty", "voice")
    bob_client = new_client()
    register(bob_client, "bob-ep")
    invite_and_join(client, bob_client, server["id"])
    carol_client = new_client()
    register(carol_client, "carol-ep")

    presence = VoicePresence(FakeSource(), make_broadcast_notify(client.app.state.comms))
    monkeypatch.setattr(client.app.state, "voice_presence", presence)
    participant = VoiceParticipant(alice_user["id"], muted=False, deafened=True)
    client.portal.call(presence.apply_snapshot, {occupied["id"]: (participant,)})

    res = bob_client.get(f"/api/voice/presence/{server['id']}")
    assert res.status_code == 200, res.text
    assert res.json() == [{
        "channel_id": occupied["id"],
        "participants": [
            {"user_id": alice_user["id"], "muted": False, "deafened": True},
        ],
    }]

    assert carol_client.get(f"/api/voice/presence/{server['id']}").status_code == 403
    assert bob_client.get("/api/voice/presence/999999").status_code == 404


def test_refresh_endpoint_rereads_the_channel_for_members(client, new_client, monkeypatch):
    alice_user = register(client, "alice-rf")
    server = create_server(client)
    voice = create_channel(client, server["id"], "Hangout", "voice")
    text = create_channel(client, server["id"], "chat")
    carol_client = new_client()
    register(carol_client, "carol-rf")

    participant = VoiceParticipant(alice_user["id"], muted=False, deafened=False)
    source = FakeSource(channels={voice["id"]: (participant,)})
    presence = VoicePresence(source, make_broadcast_notify(client.app.state.comms))
    monkeypatch.setattr(client.app.state, "voice_presence", presence)

    res = client.post(f"/api/voice/presence/channels/{voice['id']}/refresh", headers=HEADERS)
    assert res.status_code == 204, res.text
    client.portal.call(_settle, presence)
    assert source.channel_reads == [voice["id"]]
    assert presence.channel_participants(voice["id"]) == (participant,)

    url = "/api/voice/presence/channels/{}/refresh"
    assert carol_client.post(url.format(voice["id"]), headers=HEADERS).status_code == 403
    assert client.post(url.format(text["id"]), headers=HEADERS).status_code == 404
    assert client.post(url.format(999999), headers=HEADERS).status_code == 404
    assert source.channel_reads == [voice["id"]]


def test_refresh_endpoint_is_a_noop_without_livekit(client):
    register(client)
    server = create_server(client)
    voice = create_channel(client, server["id"], "Hangout", "voice")

    assert client.app.state.voice_presence is None
    res = client.post(f"/api/voice/presence/channels/{voice['id']}/refresh", headers=HEADERS)
    assert res.status_code == 204, res.text
