"""Game sessions and link codes without the HTTP layer: gain translation, the
diff rule, the resend, idle expiry and code expiry, all on a manual clock."""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.GameLink import GameLink
from app.models.GameIntegration import GameIntegration
from app.models.Server import Server
from app.permissions import Permission
from app.services.permissions import permissions
from app.services.game_sessions import (
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    IDLE_SECONDS,
    RESEND_SECONDS,
    GameSessions,
    IntegrationChanged,
    LinkCodes,
    LinkedPlayer,
    NoVoiceChannel,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeComms:
    def __init__(self):
        self.broadcasts = []
        self.sent = []

    async def broadcast_to_channel(self, server_id, channel_id, frame):
        self.broadcasts.append((server_id, channel_id, frame))

    async def send_to_user(self, user_id, frame):
        self.sent.append((user_id, frame))

    def mixes(self, user_id=None):
        return [f["gains"] for u, f in self.sent if user_id in (None, u)]

    def sessions(self):
        return [f for _, _, f in self.broadcasts]


LINKS = {
    "a": LinkedPlayer(1, "Alice"),
    "b": LinkedPlayer(2, "Bob"),
    "c": LinkedPlayer(3, "Cleo"),
}


def integration(**overrides):
    values = dict(id=7, server_id=1, voice_channel_id=9, name="Minecraft",
                  players_hear_spectators=False, key_hash="hash")
    return SimpleNamespace(**{**values, **overrides})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def comms():
    return FakeComms()


class Rows:
    """Stands in for the GameLink query so the service runs without a database."""

    def __init__(self, service):
        self.service = service

    async def values_list(self, *fields):
        self.service.loads += 1
        if self.service.on_load:
            self.service.on_load()
        return [(player_id, link.user_id, link.player_name)
                for player_id, link in self.service.links.items()]


@pytest.fixture
def sessions(comms, clock, monkeypatch):
    service = GameSessions(comms, clock)
    service.loads = 0
    service.links = dict(LINKS)
    service.on_load = None
    service.viewers = {1, 2, 3}
    service.stored = None
    asked = []

    async def effective(user_id, server, channel_id=None):
        asked.append(channel_id)
        return Permission.VIEW_CHANNEL if user_id in service.viewers else None

    async def stored(**kwargs):
        return service.stored

    async def server(**kwargs):
        return SimpleNamespace(id=kwargs["id"])

    service.asked = asked
    monkeypatch.setattr(GameLink, "filter", lambda **kwargs: Rows(service))
    monkeypatch.setattr(GameIntegration, "get_or_none", stored)
    monkeypatch.setattr(Server, "get_or_none", server)
    monkeypatch.setattr(permissions, "effective", effective)
    return service


SAME = object()


def frame(sessions, players, mix, stored=SAME, **overrides):
    """One frame. The integration the database holds is the one sent, unless *stored* says otherwise."""
    sent = integration(**overrides)
    sessions.stored = sent if stored is SAME else stored
    return asyncio.run(sessions.frame(sent, players, mix))


def test_gains_are_translated_clamped_rounded_and_filtered(sessions, comms):
    linked = frame(sessions, ["a", "b", "c", "ghost"], {
        "a": {"a": 1, "b": 0.426, "c": 7, "ghost": 1},
        "b": {"a": -3, "c": 0.004},
        "ghost": {"a": 1},
        "elsewhere": {"a": 1},
    })

    assert linked == ["a", "b", "c"]
    assert comms.mixes(1) == [{"2": 0.43, "3": 1.0}]
    assert comms.mixes(2) == [{}]
    assert comms.mixes(3) == [{}]
    assert {user_id for user_id, _ in comms.sent} == {1, 2, 3}


def test_a_listener_only_gets_their_own_gains(sessions, comms):
    frame(sessions, ["a", "b"], {"a": {"b": 0.5}, "b": {"a": 0.9}})

    by_user = {user_id: f["gains"] for user_id, f in comms.sent}
    assert by_user == {1: {"2": 0.5}, 2: {"1": 0.9}}


def test_a_linked_user_who_cannot_view_the_channel_is_treated_as_unlinked(sessions, comms, clock):
    assert frame(sessions, ["a", "b"], {"a": {"b": 1}, "b": {"a": 1}}) == ["a", "b"]
    comms.sent.clear()

    sessions.viewers.discard(2)
    clock.advance(0.2)
    linked = frame(sessions, ["a", "b"], {"a": {"b": 1}, "b": {"a": 1}})

    assert linked == ["a"]
    assert comms.mixes(2) == []
    assert comms.mixes(1) == [{}]
    assert [p["user_id"] for p in comms.sessions()[-1]["players"]] == [1]
    assert set(sessions.asked) == {9}

    sessions.viewers.add(2)
    clock.advance(0.2)
    assert frame(sessions, ["a", "b"], {"a": {"b": 1}}) == ["a", "b"]
    assert comms.mixes(1)[-1] == {"2": 1.0}


def test_players_are_deduplicated_in_the_answer_order(sessions):
    assert frame(sessions, ["b", "a", "x"], {}) == ["b", "a"]


@pytest.mark.parametrize(
    ("first", "second", "wait", "resent"),
    [
        (0.50, 0.52, 0.2, False),
        (0.50, 0.48, 0.2, False),
        (0.50, 0.53, 0.2, True),
        (0.50, 0.47, 0.2, True),
        (0.50, 0.50, RESEND_SECONDS - 0.1, False),
        (0.50, 0.50, RESEND_SECONDS, True),
        (0.50, 0.0, 0.2, True),
    ],
    ids=["small-up", "small-down", "big-up", "big-down", "before-resend", "at-resend", "to-zero"],
)
def test_diff_rule(sessions, comms, clock, first, second, wait, resent):
    frame(sessions, ["a", "b"], {"a": {"b": first}})
    clock.advance(wait)
    frame(sessions, ["a", "b"], {"a": {"b": second}})

    assert len(comms.mixes(1)) == (2 if resent else 1)


def test_drift_is_measured_from_the_last_frame_sent(sessions, comms, clock):
    frame(sessions, ["a", "b"], {"a": {"b": 0.50}})
    for gain in (0.52, 0.53):
        clock.advance(0.2)
        frame(sessions, ["a", "b"], {"a": {"b": gain}})

    assert comms.mixes(1) == [{"2": 0.5}, {"2": 0.53}]


def test_a_speaker_added_or_removed_is_sent_at_once(sessions, comms, clock):
    frame(sessions, ["a", "b", "c"], {"a": {"b": 0.5}})
    clock.advance(0.2)
    frame(sessions, ["a", "b", "c"], {"a": {"b": 0.5, "c": 0.5}})
    clock.advance(0.2)
    frame(sessions, ["a", "b", "c"], {"a": {"c": 0.5}})

    assert comms.mixes(1) == [{"2": 0.5}, {"2": 0.5, "3": 0.5}, {"3": 0.5}]


def test_a_gain_of_zero_counts_as_silent(sessions, comms, clock):
    frame(sessions, ["a", "b"], {"a": {}})
    clock.advance(0.2)
    frame(sessions, ["a", "b"], {"a": {"b": 0.0}})

    assert comms.mixes(1) == [{}]


def test_a_listener_who_returns_is_sent_a_mix_at_once(sessions, comms, clock):
    frame(sessions, ["a", "b"], {})
    clock.advance(0.2)
    frame(sessions, ["a"], {})
    clock.advance(0.2)
    frame(sessions, ["a", "b"], {})

    assert len(comms.mixes(2)) == 2
    assert len(comms.mixes(1)) == 1


def test_the_session_is_announced_on_start_and_on_player_changes_only(sessions, comms, clock):
    frame(sessions, ["a"], {})
    clock.advance(0.2)
    frame(sessions, ["a"], {})
    assert [f["players"] for f in comms.sessions()] == [[{"user_id": 1, "player_name": "Alice"}]]
    start = comms.sessions()[0]
    assert start == {
        "type": "game_session", "server_id": 1, "channel_id": 9, "integration_id": 7,
        "name": "Minecraft", "active": True, "players_hear_spectators": False,
        "players": [{"user_id": 1, "player_name": "Alice"}],
    }

    clock.advance(0.2)
    frame(sessions, ["a", "b", "ghost"], {})
    assert [p["user_id"] for p in comms.sessions()[-1]["players"]] == [1, 2]
    assert len(comms.sessions()) == 2


def test_links_are_cached_until_they_change(sessions, comms, clock):
    frame(sessions, ["a", "b"], {})
    frame(sessions, ["a", "b"], {})
    assert sessions.loads == 1

    sessions.links = {"a": LINKS["a"]}
    sessions.links_changed(7)
    clock.advance(0.2)
    assert frame(sessions, ["a", "b"], {}) == ["a"]
    assert sessions.loads == 2
    assert [p["user_id"] for p in comms.sessions()[-1]["players"]] == [1]


def test_a_link_change_during_a_load_is_not_cached(sessions):
    sessions.on_load = lambda: sessions.links_changed(7)
    frame(sessions, ["a"], {})
    assert 7 not in sessions._links

    sessions.on_load = None
    frame(sessions, ["a"], {})
    frame(sessions, ["a"], {})
    assert sessions.loads == 2


@pytest.mark.parametrize(
    ("idle", "ended"),
    [(IDLE_SECONDS - 0.1, False), (IDLE_SECONDS, True)],
    ids=["just-before", "at-ten-seconds"],
)
def test_idle_sessions_end(sessions, comms, clock, idle, ended):
    frame(sessions, ["a"], {})
    clock.advance(idle)
    asyncio.run(sessions.sweep())

    assert sessions.is_active(7) is not ended
    assert (comms.sessions()[-1]["active"] is False) is ended


def test_a_frame_keeps_the_session_alive(sessions, clock):
    frame(sessions, ["a"], {})
    clock.advance(IDLE_SECONDS - 1)
    frame(sessions, ["a"], {})
    clock.advance(IDLE_SECONDS - 1)
    asyncio.run(sessions.sweep())

    assert sessions.is_active(7)


def test_ending_broadcasts_inactive_with_no_players(sessions, comms):
    frame(sessions, ["a"], {})
    asyncio.run(sessions.end(7))
    asyncio.run(sessions.end(7))

    assert not sessions.is_active(7)
    assert sessions.session_for_channel(9) is None
    ended = comms.sessions()[1:]
    assert len(ended) == 1
    assert ended[0]["active"] is False
    assert ended[0]["players"] == []


def test_a_session_starts_over_after_it_ended(sessions, comms):
    frame(sessions, ["a", "b"], {"a": {"b": 1}})
    asyncio.run(sessions.end(7))
    frame(sessions, ["a", "b"], {"a": {"b": 1}})

    assert [f["active"] for f in comms.sessions()] == [True, False, True]
    assert comms.mixes(1) == [{"2": 1.0}, {"2": 1.0}]


def test_a_frame_for_another_channel_ends_the_session_on_the_old_one(sessions, comms):
    frame(sessions, ["a"], {})
    frame(sessions, ["a"], {}, voice_channel_id=10)

    old_end, new_start = comms.broadcasts[1:]
    assert old_end[1] == 9 and old_end[2]["active"] is False
    assert new_start[1] == 10 and new_start[2]["active"] is True
    assert sessions.session_for_channel(9) is None
    assert sessions.session_for_channel(10)["integration_id"] == 7


def test_settings_changes_reannounce_an_active_session_only(sessions, comms):
    asyncio.run(sessions.settings_changed(integration(name="Idle")))
    assert comms.broadcasts == []

    frame(sessions, ["a"], {})
    asyncio.run(sessions.settings_changed(
        integration(name="Survival", players_hear_spectators=True)))

    latest = comms.sessions()[-1]
    assert latest["name"] == "Survival"
    assert latest["players_hear_spectators"] is True
    assert sessions.session_for_channel(9) == latest


def test_a_changed_setting_seen_in_a_frame_is_announced(sessions, comms):
    frame(sessions, ["a"], {})
    frame(sessions, ["a"], {}, name="Creative")

    assert comms.sessions()[-1]["name"] == "Creative"


def test_the_sweeper_loop_survives_a_failed_sweep(sessions, monkeypatch):
    calls = []

    async def sweep():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    monkeypatch.setattr("app.services.game_sessions.SWEEP_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(sessions, "sweep", sweep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sessions.run())
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stored", [None, integration(key_hash="rotated")], ids=["deleted", "rotated"])
def test_a_frame_for_an_integration_that_changed_starts_no_session(sessions, comms, stored):
    with pytest.raises(IntegrationChanged):
        frame(sessions, ["a"], {}, stored=stored)

    assert not sessions.is_active(7)
    assert comms.broadcasts == [] and comms.sent == []


def test_a_frame_uses_the_integration_as_stored_now(sessions, comms):
    frame(sessions, ["a"], {}, stored=integration(voice_channel_id=10, name="Moved"))

    assert comms.sessions()[0]["channel_id"] == 10
    assert comms.sessions()[0]["name"] == "Moved"


def test_a_frame_for_an_integration_that_lost_its_channel_ends_the_session(sessions, comms):
    frame(sessions, ["a"], {})

    with pytest.raises(NoVoiceChannel):
        frame(sessions, ["a"], {}, voice_channel_id=None)

    assert not sessions.is_active(7)
    assert comms.sessions()[-1]["active"] is False


# --- link codes ----------------------------------------------------------

@pytest.fixture
def codes(clock):
    return LinkCodes(clock)


def test_codes_use_the_alphabet(codes):
    code = codes.create(1, "p", "Steve")

    assert len(code) == CODE_LENGTH
    assert set(code) <= set(CODE_ALPHABET)


def test_a_new_code_replaces_the_old_one_for_the_same_player(codes):
    first = codes.create(1, "p", "Steve")
    second = codes.create(1, "p", "Steve")

    assert codes.get(first) is None
    assert codes.get(second).player_id == "p"


def test_codes_for_other_players_and_integrations_are_independent(codes):
    kept = [codes.create(1, "p", "Steve"), codes.create(1, "q", "Alex"),
            codes.create(2, "p", "Steve")]

    assert all(codes.get(code) for code in kept)


@pytest.mark.parametrize(("wait", "alive"), [(CODE_TTL_SECONDS - 1, True), (CODE_TTL_SECONDS, False)])
def test_codes_expire(codes, clock, wait, alive):
    code = codes.create(1, "p", "Steve")
    clock.advance(wait)

    assert (codes.get(code) is not None) is alive


def test_expired_codes_are_purged_when_the_store_is_used(codes, clock):
    old = codes.create(1, "p", "Steve")
    clock.advance(CODE_TTL_SECONDS)
    codes.create(1, "q", "Alex")

    assert old not in codes._codes
    assert (1, "p") not in codes._by_player


def test_lookup_ignores_case_and_surrounding_space(codes):
    code = codes.create(1, "p", "Steve")

    assert codes.get(f"  {code.lower()} ").player_name == "Steve"


def test_a_code_is_used_once(codes):
    code = codes.create(1, "p", "Steve")

    assert codes.consume(code) is not None
    assert codes.consume(code) is None
    assert codes.get(code) is None


def test_forgetting_an_integration_drops_its_codes_only(codes):
    gone = codes.create(1, "p", "Steve")
    kept = codes.create(2, "p", "Steve")
    codes.forget_integration(1)

    assert codes.get(gone) is None
    assert codes.get(kept) is not None
