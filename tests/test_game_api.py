"""Game voice over HTTP and the websocket: key auth, link codes, frames and the
frames players receive. The session service's own rules are in test_game_sessions."""
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.app import app
from app.models.GameIntegration import GameIntegration
from app.routers import game as game_router
from app.permissions import Permission
from app.rate_limit import game_key, limiter
from app.services.game_sessions import CODE_ALPHABET, CODE_TTL_SECONDS, IDLE_SECONDS
from tests.conftest import ORIGIN, drain_jobs, ws_ready
from tests.test_game_integrations import bearer, integration, make_integration, team  # noqa: F401
from tests.test_game_sessions import Clock
from tests.test_permissions import roles_by_name
from tests.test_realtime_events import invite_and_join

HEADERS = {"origin": ORIGIN}
LINK_CODES = "/api/game/v1/link-codes"
FRAMES = "/api/game/v1/frames"
GAME_ENDPOINTS = [
    ("post", LINK_CODES, {"player_id": "p", "player_name": "Steve"}),
    ("delete", "/api/game/v1/links/p", None),
    ("post", FRAMES, {"players": [], "mix": {}}),
    ("delete", "/api/game/v1/session", None),
]


@pytest.fixture
def game(new_client):
    """The game server: a client with no session cookie."""
    return new_client()


@pytest.fixture
def key(integration):  # noqa: F811
    return integration["key"]


@pytest.fixture
def clock(client):
    manual = Clock()
    client.app.state.game_sessions.clock = manual
    client.app.state.link_codes.clock = manual
    return manual


@pytest.fixture
def fresh_limits():
    limiter.reset()
    yield
    limiter.reset()


def call(game, method, path, body, key):
    headers = bearer(key) if key else {}
    return getattr(game, method)(path, headers=headers, **({"json": body} if body else {}))


def send_frame(game, key, players, mix=None):
    res = game.post(FRAMES, json={"players": players, "mix": mix or {}}, headers=bearer(key))
    assert res.status_code == 200, res.text
    return res.json()


def link_player(game, key, user_client, player_id, name=None):
    created = game.post(
        LINK_CODES, json={"player_id": player_id, "player_name": name or player_id.title()},
        headers=bearer(key))
    assert created.status_code == 201, created.text
    claimed = user_client.post(f"/api/game/link-codes/{created.json()['code']}/claim")
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


@pytest.fixture
def players(client, game, team, key):  # noqa: F811
    """The owner is p1 and the member is p2, both linked."""
    link_player(game, key, client, "p1")
    link_player(game, key, team["member_client"], "p2")


@pytest.fixture
def sockets(client, team):  # noqa: F811
    """Opens a websocket for "owner", "member" or "manager"."""
    users = {"owner": client, "member": team["member_client"], "manager": team["manager_client"]}
    with ExitStack() as stack:
        def open_socket(name):
            ws = stack.enter_context(users[name].websocket_connect("/ws", headers=HEADERS))
            ws_ready(ws)
            return ws

        yield open_socket


def received(client, ws, user_id):
    """The game frames that reached `ws` so far, found by chasing them with a marker."""
    client.portal.call(client.app.state.comms.send_to_user, user_id, {"type": "marker"})
    frames = []
    while (frame := ws.receive_json())["type"] != "marker":
        if frame["type"].startswith("game_"):
            frames.append(frame)
    return frames


def sessions_of(frames):
    return [f for f in frames if f["type"] == "game_session"]


def mixes_of(frames):
    return [f for f in frames if f["type"] == "game_mix"]


# --- key auth ------------------------------------------------------------

@pytest.mark.parametrize(("method", "path", "body"), GAME_ENDPOINTS,
                         ids=["link-code", "unlink", "frame", "end-session"])
@pytest.mark.parametrize("header", [None, "Bearer zgk_wrong", "Bearer", "Basic abc", "zgk_x"])
def test_the_game_api_needs_a_known_key(game, method, path, body, header):
    headers = {"Authorization": header} if header else {}

    res = getattr(game, method)(path, headers=headers, **({"json": body} if body else {}))

    assert (res.status_code, res.json()) == (401, {"detail": "Invalid game key"})


@pytest.mark.parametrize(("method", "path", "body"), GAME_ENDPOINTS,
                         ids=["link-code", "unlink", "frame", "end-session"])
def test_a_rotated_key_stops_working(client, game, integration, method, path, body):  # noqa: F811
    old = integration["key"]
    assert call(game, method, path, body, old).status_code in (201, 200, 204, 404)

    new = client.post(f"/api/game-integrations/{integration['id']}/key").json()["key"]

    assert call(game, method, path, body, old).status_code == 401
    assert call(game, method, path, body, new).status_code in (201, 200, 204, 404)


def test_the_cookie_session_is_not_a_game_key(client, team, integration):  # noqa: F811
    res = client.post(FRAMES, json={"players": []})
    assert res.status_code == 401


def test_the_key_is_limited_per_integration_not_per_address(
        monkeypatch, game, team, key, fresh_limits, client):  # noqa: F811
    other = make_integration(client, team["server"]["id"], team["lounge"]["id"], name="Other")
    monkeypatch.setenv("GAME_FRAME_RATE_LIMIT", "3/minute")

    statuses = [game.post(FRAMES, json={"players": []}, headers=bearer(key)).status_code
                for _ in range(4)]
    other_status = game.post(
        FRAMES, json={"players": []}, headers=bearer(other["key"])).status_code

    assert statuses == [200, 200, 200, 429]
    assert other_status == 200


def test_failed_authentication_is_limited_per_address(
        monkeypatch, game, key, fresh_limits):  # noqa: F811
    monkeypatch.setenv("GAME_AUTH_FAIL_RATE_LIMIT", "3/minute")
    for _ in range(5):
        assert game.post(FRAMES, json={"players": []}, headers=bearer(key)).status_code == 200

    failures = [
        game.post(FRAMES, json={"players": []}, headers=headers)
        for headers in (bearer("zgk_wrong"), {}, bearer("zgk_wrong"), bearer("zgk_wrong"))]

    assert [res.status_code for res in failures] == [401, 401, 401, 429]
    assert int(failures[-1].headers["Retry-After"]) > 0
    assert game.post(FRAMES, json={"players": []}, headers=bearer(key)).status_code == 200


def request_from(address, authorization=None):
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "headers": headers, "client": (address, 1234)})


def test_the_rate_limit_key_is_the_key_hash_or_else_the_address():
    token = "Bearer zgk_abc"

    assert game_key(request_from("1.1.1.1", token)) == game_key(request_from("2.2.2.2", token))
    assert game_key(request_from("1.1.1.1", token)) != game_key(
        request_from("1.1.1.1", "Bearer zgk_other"))
    assert "zgk_abc" not in game_key(request_from("1.1.1.1", token))
    assert game_key(request_from("1.1.1.1")) == "1.1.1.1"


# --- link codes ----------------------------------------------------------

def test_a_game_asks_for_a_code_and_a_link(game, key):
    res = game.post(LINK_CODES, json={"player_id": "uuid-1", "player_name": "Steve"},
                    headers=bearer(key))

    assert res.status_code == 201
    body = res.json()
    assert len(body["code"]) == 8 and set(body["code"]) <= set(CODE_ALPHABET)
    assert body["url"] == f"{ORIGIN}/app/link-game?code={body['code']}"
    assert body["expires_in"] == 600


def test_app_url_sets_the_link_base(monkeypatch, game, key):
    monkeypatch.setenv("APP_URL", "https://zeta.example/")

    res = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                    headers=bearer(key))

    assert res.json()["url"].startswith("https://zeta.example/app/link-game?code=")


@pytest.mark.parametrize("body", [
    {"player_id": "", "player_name": "Steve"},
    {"player_id": "p", "player_name": ""},
    {"player_id": "x" * 65, "player_name": "Steve"},
    {"player_id": "p", "player_name": "x" * 65},
    {"player_id": "p"},
])
def test_link_code_requests_are_validated(game, key, body):
    assert game.post(LINK_CODES, json=body, headers=bearer(key)).status_code == 422


def test_a_new_code_replaces_the_old_one(game, client, key):
    body = {"player_id": "p", "player_name": "Steve"}
    first = game.post(LINK_CODES, json=body, headers=bearer(key)).json()["code"]
    second = game.post(LINK_CODES, json=body, headers=bearer(key)).json()["code"]

    assert client.get(f"/api/game/link-codes/{first}").status_code == 404
    assert client.get(f"/api/game/link-codes/{second}").status_code == 200


def test_preview_describes_the_link(game, team, integration, key):  # noqa: F811
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]

    res = team["member_client"].get(f"/api/game/link-codes/{code}")

    assert res.status_code == 200
    assert res.json() == {
        "integration_id": integration["id"],
        "integration_name": "Minecraft", "server_id": team["server"]["id"],
        "server_name": team["server"]["name"], "player_name": "Steve",
        "linked_player_name": None,
    }


def test_preview_names_the_account_the_user_already_linked(client, game, key):
    link_player(game, key, client, "p1", "Steve")
    code = game.post(LINK_CODES, json={"player_id": "p2", "player_name": "Alex"},
                     headers=bearer(key)).json()["code"]

    res = client.get(f"/api/game/link-codes/{code}")

    assert (res.json()["player_name"], res.json()["linked_player_name"]) == ("Alex", "Steve")


def test_claiming_links_the_account_and_uses_up_the_code(client, game, team, integration, key):  # noqa: F811
    code = game.post(LINK_CODES, json={"player_id": "p1", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]

    res = client.post(f"/api/game/link-codes/{code}/claim")

    assert res.status_code == 200
    link = res.json()
    assert link.pop("created_at")
    assert link == {"user_id": team["owner"]["id"], "username": team["owner"]["username"],
                    "player_id": "p1", "player_name": "Steve"}
    assert client.post(f"/api/game/link-codes/{code}/claim").status_code == 404
    assert client.get(f"/api/game/link-codes/{code}").status_code == 404
    assert list(linked_players(client, integration["id"])) == ["p1"]


def test_codes_work_in_any_case(client, game, key):
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]

    assert client.get(f"/api/game/link-codes/{code.lower()}").status_code == 200
    assert client.post(f"/api/game/link-codes/{code.lower()}/claim").status_code == 200


@pytest.mark.parametrize(("wait", "status"), [(CODE_TTL_SECONDS - 1, 200), (CODE_TTL_SECONDS, 404)])
def test_codes_expire_after_ten_minutes(client, game, key, clock, wait, status):
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]
    clock.advance(wait)

    assert client.get(f"/api/game/link-codes/{code}").status_code == status
    detail = client.post(f"/api/game/link-codes/{code}/claim")
    assert detail.status_code == status


def test_an_unknown_code_is_not_found(client, team):  # noqa: F811
    for res in (client.get("/api/game/link-codes/NOPE2345"),
                client.post("/api/game/link-codes/NOPE2345/claim")):
        assert (res.status_code, res.json()["detail"]) == (404, "Code not found or expired")


def test_non_members_cannot_look_up_or_claim(client, game, team, key):  # noqa: F811
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]
    outsider = team["outsider_client"]

    for res in (outsider.get(f"/api/game/link-codes/{code}"),
                outsider.post(f"/api/game/link-codes/{code}/claim")):
        assert (res.status_code, res.json()["detail"]) == (403, "Not a member of this server")
    assert client.post(f"/api/game/link-codes/{code}/claim").status_code == 200


def test_signed_out_callers_cannot_use_codes(game, team, key):  # noqa: F811
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]
    anonymous = team["anonymous"]

    assert anonymous.get(f"/api/game/link-codes/{code}").status_code == 401
    assert anonymous.post(f"/api/game/link-codes/{code}/claim").status_code == 401


def linked_players(client, integration_id):
    return {l["player_id"]: l["username"] for l in client.get(
        f"/api/game-integrations/{integration_id}/links").json()}


def test_claiming_replaces_the_users_earlier_link(client, game, integration, key):  # noqa: F811
    link_player(game, key, client, "p1")
    link_player(game, key, client, "p2")

    assert list(linked_players(client, integration["id"])) == ["p2"]


def test_claiming_takes_a_player_from_whoever_held_it(client, game, team, integration, key):  # noqa: F811
    link_player(game, key, team["member_client"], "p1")
    link_player(game, key, client, "p1")

    assert linked_players(client, integration["id"]) == {"p1": team["owner"]["username"]}


def test_links_are_per_integration(client, game, team, integration, key):  # noqa: F811
    other = make_integration(client, team["server"]["id"], team["lounge"]["id"], name="Other")
    link_player(game, key, client, "p1")
    link_player(game, other["key"], client, "p1")

    assert list(linked_players(client, integration["id"])) == ["p1"]
    assert list(linked_players(client, other["id"])) == ["p1"]


def test_the_game_unlinks_a_player(client, game, integration, key):  # noqa: F811
    link_player(game, key, client, "p1")

    res = game.delete("/api/game/v1/links/p1", headers=bearer(key))
    again = game.delete("/api/game/v1/links/p1", headers=bearer(key))

    assert res.status_code == 204
    assert (again.status_code, again.json()) == (404, {"detail": "Not linked"})
    assert linked_players(client, integration["id"]) == {}


def test_link_code_lookups_are_limited_per_user(monkeypatch, client, team, fresh_limits):  # noqa: F811
    monkeypatch.setenv("GAME_LINK_RATE_LIMIT", "3/minute")

    own = [client.get("/api/game/link-codes/NOPE2345").status_code for _ in range(4)]
    other = team["member_client"].get("/api/game/link-codes/NOPE2345").status_code

    assert own == [404, 404, 404, 429]
    assert other == 404


# --- frames --------------------------------------------------------------

def test_a_frame_answers_with_the_linked_players(game, players, key):
    result = send_frame(game, key, ["p2", "stranger", "p1", "p2"])

    assert result == {"linked": ["p2", "p1"]}


def test_a_frame_before_anyone_links_links_nobody(game, key):
    assert send_frame(game, key, ["a", "b"]) == {"linked": []}


def test_a_link_is_picked_up_by_the_next_frame(client, game, key):
    assert send_frame(game, key, ["p1"]) == {"linked": []}

    link_player(game, key, client, "p1")

    assert send_frame(game, key, ["p1"]) == {"linked": ["p1"]}


def test_an_unlink_is_picked_up_by_the_next_frame(client, game, key):
    link_player(game, key, client, "p1")
    assert send_frame(game, key, ["p1"]) == {"linked": ["p1"]}

    game.delete("/api/game/v1/links/p1", headers=bearer(key))

    assert send_frame(game, key, ["p1"]) == {"linked": []}


def test_two_hundred_players_fit_and_more_do_not(game, key):
    ids = [f"p{i}" for i in range(201)]

    assert game.post(FRAMES, json={"players": ids[:200]}, headers=bearer(key)).status_code == 200
    assert game.post(FRAMES, json={"players": ids}, headers=bearer(key)).status_code == 422
    assert game.post(
        FRAMES, json={"players": ids[:150] * 2}, headers=bearer(key)).status_code == 200


@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    b'{"mix": {}}',
    b'{"players": "p1"}',
    b'{"players": [], "mix": {"a": 1}}',
    b'{"players": [], "mix": {"a": {"b": "loud"}}}',
])
def test_malformed_frames_are_rejected(game, key, body):
    res = game.post(FRAMES, content=body, headers={**bearer(key), "content-type": "application/json"})

    assert res.status_code == 422


def test_frames_over_512kb_are_refused(game, key):
    padding = b" " * (512 * 1024)
    headers = {**bearer(key), "content-type": "application/json"}

    declared = game.post(FRAMES, content=b'{"players": []}' + padding, headers=headers)
    exact = game.post(FRAMES, content=b'{"players": []}' + padding[:-20], headers=headers)

    assert (declared.status_code, exact.status_code) == (413, 200)


def test_frames_over_512kb_are_refused_without_a_content_length(game, key):
    small = game.post(FRAMES, content=iter([b'{"players": []}']),
                      headers={**bearer(key), "content-type": "application/json"})
    big = game.post(FRAMES, content=(b" " * 8192 for _ in range(65)),
                    headers={**bearer(key), "content-type": "application/json"})

    assert small.status_code == 200
    assert big.status_code == 413


def test_a_frame_without_a_channel_is_a_conflict_and_ends_the_session(
        client, game, team, players, integration, key):  # noqa: F811
    send_frame(game, key, ["p1"])
    assert client.get(f"/api/game/sessions/{team['lobby']['id']}").status_code == 200

    assert client.delete(f"/channels/{team['lobby']['id']}").status_code == 204
    res = game.post(FRAMES, json={"players": ["p1"]}, headers=bearer(key))

    assert (res.status_code, res.json()) == (409, {"detail": "No voice channel set"})
    assert not client.app.state.game_sessions.is_active(integration["id"])
    assert client.app.state.game_sessions.session_for_channel(team["lobby"]["id"]) is None


# --- sessions and what players receive -----------------------------------

def test_the_session_is_announced_when_it_starts_changes_and_ends(
        client, game, team, players, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    owner_id = team["owner"]["id"]
    channel_id = team["lobby"]["id"]

    send_frame(game, key, ["p1"])
    started = sessions_of(received(client, owner_ws, owner_id))
    send_frame(game, key, ["p1"])
    unchanged = sessions_of(received(client, owner_ws, owner_id))
    send_frame(game, key, ["p1", "p2", "stranger"])
    joined = sessions_of(received(client, owner_ws, owner_id))
    assert game.delete("/api/game/v1/session", headers=bearer(key)).status_code == 204
    ended = sessions_of(received(client, owner_ws, owner_id))

    base = {"type": "game_session", "server_id": team["server"]["id"], "channel_id": channel_id,
            "integration_id": started[0]["integration_id"], "name": "Minecraft",
            "players_hear_spectators": False}
    owner_player = {"user_id": owner_id, "player_name": "P1"}
    member_player = {"user_id": team["member"]["id"], "player_name": "P2"}
    assert started == [{**base, "active": True, "players": [owner_player]}]
    assert unchanged == []
    assert joined == [{**base, "active": True, "players": [owner_player, member_player]}]
    assert ended == [{**base, "active": False, "players": []}]


def test_game_mix_goes_only_to_the_listener(client, game, team, players, key, sockets):  # noqa: F811
    owner_ws, member_ws, manager_ws = sockets("owner"), sockets("member"), sockets("manager")
    owner_id, member_id = team["owner"]["id"], team["member"]["id"]

    send_frame(game, key, ["p1", "p2"], {"p1": {"p2": 0.426}, "p2": {"p1": 9}})

    owner_frames = received(client, owner_ws, owner_id)
    member_frames = received(client, member_ws, member_id)
    spectator_frames = received(client, manager_ws, team["manager"]["id"])
    integration_id = sessions_of(owner_frames)[0]["integration_id"]
    assert mixes_of(owner_frames) == [{
        "type": "game_mix", "channel_id": team["lobby"]["id"],
        "integration_id": integration_id, "gains": {str(member_id): 0.43}}]
    assert mixes_of(member_frames) == [{
        "type": "game_mix", "channel_id": team["lobby"]["id"],
        "integration_id": integration_id, "gains": {str(owner_id): 1.0}}]
    assert mixes_of(spectator_frames) == []
    assert len(sessions_of(spectator_frames)) == 1


def test_a_player_hears_nothing_new_while_the_mix_holds(client, game, team, players, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1", "p2"], {"p1": {"p2": 0.5}})
    received(client, owner_ws, team["owner"]["id"])

    send_frame(game, key, ["p1", "p2"], {"p1": {"p2": 0.51}})

    assert mixes_of(received(client, owner_ws, team["owner"]["id"])) == []


def test_name_and_spectator_changes_reannounce_an_active_session(
        client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])

    res = client.patch(f"/api/game-integrations/{integration['id']}",
                       json={"name": "Survival", "players_hear_spectators": True})
    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))

    assert res.json()["active"] is True
    assert [(f["name"], f["players_hear_spectators"], f["active"]) for f in frames] == [
        ("Survival", True, True)]


def test_settings_changes_announce_nothing_without_a_session(
        client, team, integration, sockets):  # noqa: F811
    owner_ws = sockets("owner")

    client.patch(f"/api/game-integrations/{integration['id']}", json={"name": "Survival"})

    assert received(client, owner_ws, team["owner"]["id"]) == []


def test_changing_the_channel_ends_the_session_on_the_old_one(
        client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])

    res = client.patch(f"/api/game-integrations/{integration['id']}",
                       json={"voice_channel_id": team["lounge"]["id"]})
    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))

    assert res.json()["active"] is False
    assert [(f["channel_id"], f["active"]) for f in frames] == [(team["lobby"]["id"], False)]
    send_frame(game, key, ["p1"])
    assert client.get(f"/api/game/sessions/{team['lobby']['id']}").status_code == 204
    assert client.get(f"/api/game/sessions/{team['lounge']['id']}").status_code == 200


def test_keeping_the_same_channel_keeps_the_session(client, game, team, players, integration, key):  # noqa: F811
    send_frame(game, key, ["p1"])

    client.patch(f"/api/game-integrations/{integration['id']}",
                 json={"voice_channel_id": team["lobby"]["id"]})

    assert client.get(f"/api/game/sessions/{team['lobby']['id']}").status_code == 200


def test_rotating_the_key_ends_the_session(client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])

    client.post(f"/api/game-integrations/{integration['id']}/key")

    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))
    assert [f["active"] for f in frames] == [False]
    assert game.post(FRAMES, json={"players": []}, headers=bearer(key)).status_code == 401


def test_rotating_the_key_voids_codes_minted_with_the_old_one(client, game, integration, key):  # noqa: F811
    code = game.post(LINK_CODES, json={"player_id": "p", "player_name": "Steve"},
                     headers=bearer(key)).json()["code"]

    client.post(f"/api/game-integrations/{integration['id']}/key")

    assert client.get(f"/api/game/link-codes/{code}").status_code == 404
    assert client.post(f"/api/game/link-codes/{code}/claim").status_code == 404


def test_deleting_the_integration_ends_the_session_and_forgets_its_codes(
        client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])
    code = game.post(LINK_CODES, json={"player_id": "p3", "player_name": "Alex"},
                     headers=bearer(key)).json()["code"]

    client.delete(f"/api/game-integrations/{integration['id']}")

    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))
    assert [(f["channel_id"], f["active"]) for f in frames] == [(team["lobby"]["id"], False)]
    assert client.get(f"/api/game/sessions/{team['lobby']['id']}").status_code == 204
    assert client.get(f"/api/game/link-codes/{code}").status_code == 404
    assert client.app.state.link_codes.get(code) is None
    assert game.post(FRAMES, json={"players": []}, headers=bearer(key)).status_code == 401


def test_unlinking_oneself_updates_the_session_on_the_next_frame(
        client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1", "p2"])
    received(client, owner_ws, team["owner"]["id"])

    res = team["member_client"].delete(
        f"/api/game-integrations/{integration['id']}/links/{team['member']['id']}")
    result = send_frame(game, key, ["p1", "p2"])
    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))

    assert res.status_code == 204
    assert result == {"linked": ["p1"]}
    assert [[p["user_id"] for p in f["players"]] for f in frames] == [[team["owner"]["id"]]]


def test_a_kicked_member_drops_out_of_the_mix_and_keeps_the_link(
        client, game, team, players, integration, key, sockets):  # noqa: F811
    owner_ws, member_ws = sockets("owner"), sockets("member")
    owner_id, member_id = team["owner"]["id"], team["member"]["id"]
    mix = {"p1": {"p2": 1}, "p2": {"p1": 1}}
    send_frame(game, key, ["p1", "p2"], mix)
    received(client, owner_ws, owner_id)
    received(client, member_ws, member_id)

    kicked = client.delete(f"/servers/{team['server']['id']}/members/{member_id}")
    result = send_frame(game, key, ["p1", "p2"], mix)
    owner_frames = received(client, owner_ws, owner_id)

    assert kicked.status_code == 204
    assert result == {"linked": ["p1"]}
    assert [[p["user_id"] for p in f["players"]] for f in sessions_of(owner_frames)] == [[owner_id]]
    assert mixes_of(received(client, member_ws, member_id)) == []
    assert list(linked_players(client, integration["id"])) == ["p1", "p2"]

    invite_and_join(client, team["member_client"], team["server"]["id"])
    assert send_frame(game, key, ["p1", "p2"], mix) == {"linked": ["p1", "p2"]}


def test_a_player_who_cannot_view_the_channel_is_left_out(
        client, game, team, players, key, sockets):  # noqa: F811
    owner_ws, member_ws = sockets("owner"), sockets("member")
    owner_id, member_id = team["owner"]["id"], team["member"]["id"]
    mix = {"p1": {"p2": 1}, "p2": {"p1": 1}}
    channel_id = team["lobby"]["id"]
    everyone = roles_by_name(client, team["server"]["id"])["@everyone"]
    hidden = client.put(
        f"/channels/{channel_id}/permissions/roles/{everyone['id']}",
        json={"allow": "0", "deny": str(int(Permission.VIEW_CHANNEL))})
    assert hidden.status_code == 200, hidden.text
    drain_jobs(client)
    received(client, owner_ws, owner_id)
    received(client, member_ws, member_id)

    result = send_frame(game, key, ["p1", "p2"], mix)

    assert result == {"linked": ["p1"]}
    assert mixes_of(received(client, owner_ws, owner_id)) == [{
        "type": "game_mix", "channel_id": channel_id,
        "integration_id": client.app.state.game_sessions.session_for_channel(channel_id)["integration_id"],
        "gains": {}}]
    assert mixes_of(received(client, member_ws, member_id)) == []


def test_claiming_a_link_joins_the_session_on_the_next_frame(
        client, game, team, integration, key, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    assert [f["players"] for f in sessions_of(received(client, owner_ws, team["owner"]["id"]))] == [[]]

    link_player(game, key, client, "p1")
    send_frame(game, key, ["p1"])

    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))
    assert [[p["user_id"] for p in f["players"]] for f in frames] == [[team["owner"]["id"]]]


def test_a_game_that_goes_quiet_loses_its_session_after_ten_seconds(
        client, game, team, players, key, clock, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])
    lookup = f"/api/game/sessions/{team['lobby']['id']}"

    clock.advance(IDLE_SECONDS - 1)
    client.portal.call(client.app.state.game_sessions.sweep)
    assert client.get(lookup).status_code == 200
    assert received(client, owner_ws, team["owner"]["id"]) == []

    clock.advance(1)
    client.portal.call(client.app.state.game_sessions.sweep)
    frames = sessions_of(received(client, owner_ws, team["owner"]["id"]))
    assert [(f["channel_id"], f["active"]) for f in frames] == [(team["lobby"]["id"], False)]
    assert client.get(lookup).status_code == 204


@pytest.mark.parametrize("change", ["rotated", "deleted"])
def test_a_frame_in_flight_when_the_integration_changes_starts_no_session(
        monkeypatch, client, game, team, players, integration, key, change):  # noqa: F811
    read_limited = game_router.read_limited

    async def read_then_change(request, limit):
        body = await read_limited(request, limit)
        if change == "rotated":
            await GameIntegration.filter(id=integration["id"]).update(key_hash="0" * 64)
        else:
            await GameIntegration.filter(id=integration["id"]).delete()
        return body

    monkeypatch.setattr(game_router, "read_limited", read_then_change)

    res = game.post(FRAMES, json={"players": ["p1"]}, headers=bearer(key))

    assert (res.status_code, res.json()) == (409, {"detail": "Integration changed, retry"})
    assert client.get(f"/api/game/sessions/{team['lobby']['id']}").status_code == 204


def test_the_session_lookup_returns_the_frame_body_or_nothing(
        client, game, team, players, key):  # noqa: F811
    channel_id = team["lobby"]["id"]
    assert client.get(f"/api/game/sessions/{channel_id}").status_code == 204

    send_frame(game, key, ["p1", "p2"])
    res = team["member_client"].get(f"/api/game/sessions/{channel_id}")

    assert res.status_code == 200
    assert res.json() == {
        "type": "game_session", "server_id": team["server"]["id"], "channel_id": channel_id,
        "integration_id": res.json()["integration_id"], "name": "Minecraft", "active": True,
        "players_hear_spectators": False,
        "players": [{"user_id": team["owner"]["id"], "player_name": "P1"},
                    {"user_id": team["member"]["id"], "player_name": "P2"}],
    }


def test_the_session_lookup_hides_channels_the_caller_cannot_view(client, game, team, players, key):  # noqa: F811
    send_frame(game, key, ["p1"])
    channel_id = team["lobby"]["id"]

    assert team["outsider_client"].get(f"/api/game/sessions/{channel_id}").status_code == 404
    assert team["anonymous"].get(f"/api/game/sessions/{channel_id}").status_code == 401
    assert client.get("/api/game/sessions/999999").status_code == 404


def test_the_session_lookup_hides_channels_a_rule_hides(client, game, team, players, key):  # noqa: F811
    send_frame(game, key, ["p1"])
    channel_id = team["lobby"]["id"]
    everyone = roles_by_name(client, team["server"]["id"])["@everyone"]
    hidden = client.put(
        f"/channels/{channel_id}/permissions/roles/{everyone['id']}",
        json={"allow": "0", "deny": str(int(Permission.VIEW_CHANNEL))})
    assert hidden.status_code == 200, hidden.text
    drain_jobs(client)

    assert team["member_client"].get(f"/api/game/sessions/{channel_id}").status_code == 404
    assert client.get(f"/api/game/sessions/{channel_id}").status_code == 200


def test_the_sweeper_runs_with_the_app_and_stops_with_it():
    with TestClient(app) as started:
        task = started.app.state.game_task
        assert not task.done()
    assert task.cancelled()


def test_the_running_sweeper_ends_idle_sessions(
        client, game, team, players, key, clock, sockets):  # noqa: F811
    owner_ws = sockets("owner")
    send_frame(game, key, ["p1"])
    received(client, owner_ws, team["owner"]["id"])

    clock.advance(IDLE_SECONDS)
    frame = owner_ws.receive_json()
    while frame["type"] != "game_session":
        frame = owner_ws.receive_json()

    assert frame["active"] is False
