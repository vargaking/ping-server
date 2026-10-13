"""Game integrations: the owner API, who may use it, and the keys."""
import pytest

from app.models.GameIntegration import GameIntegration
from app.models.GameLink import GameLink
from app.permissions import Permission
from app.settings import app_url
from tests.conftest import create_channel, create_server, register
from tests.test_permissions import join, set_roles

PREFIX = "zgk_"


def db(client, query):
    """Run `query()`, which returns an awaitable, on the app's event loop."""
    async def run():
        return await query()

    return client.portal.call(run)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def make_integration(client, server_id, channel_id, **fields):
    res = client.post(
        f"/api/servers/{server_id}/game-integrations",
        json={"name": "Minecraft", "voice_channel_id": channel_id, **fields})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def team(client, new_client):
    """An owner (the `client`), a plain member, a manager holding only MANAGE_SERVER,
    and someone outside the server who owns a server with a voice channel of their own."""
    owner = register(client)
    server = create_server(client)
    text = create_channel(client, server["id"], "general")
    lobby = create_channel(client, server["id"], "Lobby", "voice")
    lounge = create_channel(client, server["id"], "Lounge", "voice")
    member_client, member = join(client, new_client, server["id"])
    manager_client, manager = join(client, new_client, server["id"])
    role = client.post(
        f"/servers/{server['id']}/roles",
        json={"name": "Managers", "allow": str(int(Permission.MANAGE_SERVER))})
    assert role.status_code == 201, role.text
    assert set_roles(client, server["id"], manager["id"], [role.json()["id"]]).status_code == 200

    outsider_client = new_client()
    outsider = register(outsider_client)
    elsewhere = create_server(outsider_client, "Elsewhere")
    foreign_voice = create_channel(outsider_client, elsewhere["id"], "Away", "voice")
    return {
        "owner": owner, "server": server, "text": text, "lobby": lobby, "lounge": lounge,
        "member_client": member_client, "member": member,
        "manager_client": manager_client, "manager": manager,
        "outsider_client": outsider_client, "outsider": outsider,
        "foreign_voice": foreign_voice, "anonymous": new_client(),
    }


@pytest.fixture
def integration(client, team):
    return make_integration(client, team["server"]["id"], team["lobby"]["id"])


def owner_calls(team, integration):
    ids = {"server": team["server"]["id"], "integration": integration["id"],
           "owner": team["owner"]["id"]}
    lobby = team["lobby"]["id"]
    return [
        ("get", "/api/servers/{server}/game-integrations".format(**ids), None),
        ("post", "/api/servers/{server}/game-integrations".format(**ids),
         {"name": "Other", "voice_channel_id": lobby}),
        ("patch", "/api/game-integrations/{integration}".format(**ids), {"name": "Renamed"}),
        ("post", "/api/game-integrations/{integration}/key".format(**ids), None),
        ("delete", "/api/game-integrations/{integration}".format(**ids), None),
        ("get", "/api/game-integrations/{integration}/links".format(**ids), None),
        ("delete", "/api/game-integrations/{integration}/links/{owner}".format(**ids), None),
    ]


@pytest.mark.parametrize("who", ["member_client", "outsider_client"])
def test_only_managers_reach_the_owner_api(team, integration, who):
    for method, path, body in owner_calls(team, integration):
        res = getattr(team[who], method)(path, **({"json": body} if body else {}))
        assert res.status_code == 404, (method, path, res.text)
        assert res.json()["detail"] in ("Server not found", "Integration not found")

    listed = team["manager_client"].get(
        f"/api/servers/{team['server']['id']}/game-integrations").json()
    assert [(i["id"], i["name"]) for i in listed] == [(integration["id"], "Minecraft")]


def test_unknown_integrations_are_not_found(client):
    register(client)
    for method, path in [("patch", ""), ("post", "/key"), ("delete", ""), ("get", "/links")]:
        res = getattr(client, method)(f"/api/game-integrations/999{path}",
                                      **({"json": {}} if method == "patch" else {}))
        assert (res.status_code, res.json()["detail"]) == (404, "Integration not found")


def test_signed_out_callers_are_refused(team, integration):
    for method, path, body in owner_calls(team, integration):
        res = getattr(team["anonymous"], method)(path, **({"json": body} if body else {}))
        assert res.status_code == 401, (method, path)


def test_a_manager_can_run_the_whole_lifecycle(team):
    manager = team["manager_client"]
    sid = team["server"]["id"]
    created = make_integration(manager, sid, team["lobby"]["id"])

    assert manager.patch(f"/api/game-integrations/{created['id']}",
                         json={"name": "Survival"}).json()["name"] == "Survival"
    assert manager.post(f"/api/game-integrations/{created['id']}/key").status_code == 200
    assert manager.delete(f"/api/game-integrations/{created['id']}").status_code == 204


def test_create_returns_the_key_once(client, team):
    sid = team["server"]["id"]
    created = make_integration(
        client, sid, team["lobby"]["id"], name="  Minecraft  ", players_hear_spectators=True)

    key = created.pop("key")
    assert key.startswith(PREFIX) and len(key) > 40
    assert created["key_prefix"] == key[len(PREFIX):len(PREFIX) + 8]
    assert created.pop("created_at")
    assert created == {
        "id": created["id"], "server_id": sid, "name": "Minecraft",
        "voice_channel_id": team["lobby"]["id"], "players_hear_spectators": True,
        "key_prefix": created["key_prefix"], "linked_count": 0, "active": False,
    }

    listed = client.get(f"/api/servers/{sid}/game-integrations").json()
    assert [i["id"] for i in listed] == [created["id"]]
    assert "key" not in listed[0]
    assert key not in str(listed)


def test_only_the_hash_of_the_key_is_stored(client, team):
    created = make_integration(client, team["server"]["id"], team["lobby"]["id"])

    row = db(client, lambda: GameIntegration.get(id=created["id"]))
    assert row.key_hash != created["key"]
    assert created["key"] not in (row.key_hash, row.key_prefix)
    assert len(row.key_hash) == 64


def test_players_hear_spectators_defaults_to_off(integration):
    assert integration["players_hear_spectators"] is False


@pytest.mark.parametrize("name", ["", "   ", "x" * 33])
def test_names_are_one_to_thirty_two_characters(client, team, name):
    res = client.post(
        f"/api/servers/{team['server']['id']}/game-integrations",
        json={"name": name, "voice_channel_id": team["lobby"]["id"]})
    assert res.status_code == 422


@pytest.mark.parametrize("channel", ["text", "foreign_voice", "missing"])
def test_the_channel_must_be_a_voice_channel_of_the_server(client, team, integration, channel):
    channel_id = 99999 if channel == "missing" else team[channel]["id"]

    created = client.post(
        f"/api/servers/{team['server']['id']}/game-integrations",
        json={"name": "Minecraft", "voice_channel_id": channel_id})
    patched = client.patch(
        f"/api/game-integrations/{integration['id']}", json={"voice_channel_id": channel_id})

    for res in (created, patched):
        assert (res.status_code, res.json()["detail"]) == (
            400, "Pick a voice channel of this server")
    assert client.get(
        f"/api/servers/{team['server']['id']}/game-integrations"
    ).json()[0]["voice_channel_id"] == team["lobby"]["id"]


def test_the_channel_cannot_be_cleared(client, integration):
    res = client.patch(
        f"/api/game-integrations/{integration['id']}", json={"voice_channel_id": None})
    assert res.status_code == 400


def test_a_server_holds_five_integrations(client, team):
    sid = team["server"]["id"]
    for index in range(5):
        make_integration(client, sid, team["lobby"]["id"], name=f"Game {index}")

    res = client.post(
        f"/api/servers/{sid}/game-integrations",
        json={"name": "Sixth", "voice_channel_id": team["lobby"]["id"]})
    assert res.status_code == 400
    assert "5" in res.json()["detail"]
    assert len(client.get(f"/api/servers/{sid}/game-integrations").json()) == 5

    other = create_server(client, "Another")
    voice = create_channel(client, other["id"], "Voice", "voice")
    make_integration(client, other["id"], voice["id"])


def test_patch_changes_only_what_it_is_given(client, team, integration):
    res = client.patch(
        f"/api/game-integrations/{integration['id']}",
        json={"name": "Creative", "players_hear_spectators": True})
    assert res.status_code == 200
    assert res.json()["name"] == "Creative"
    assert res.json()["players_hear_spectators"] is True
    assert res.json()["voice_channel_id"] == team["lobby"]["id"]

    moved = client.patch(
        f"/api/game-integrations/{integration['id']}",
        json={"voice_channel_id": team["lounge"]["id"]}).json()
    assert moved["voice_channel_id"] == team["lounge"]["id"]
    assert moved["name"] == "Creative"
    assert "key" not in moved


def test_rotating_returns_a_new_key_and_prefix(client, team, integration):
    res = client.post(f"/api/game-integrations/{integration['id']}/key")

    assert res.status_code == 200
    rotated = res.json()
    assert set(rotated) == {"key", "key_prefix"}
    assert rotated["key"] != integration["key"]
    assert rotated["key_prefix"] == rotated["key"][len(PREFIX):len(PREFIX) + 8]
    listed = client.get(f"/api/servers/{team['server']['id']}/game-integrations").json()[0]
    assert listed["key_prefix"] == rotated["key_prefix"]
    assert "key" not in listed


def test_deleting_removes_the_integration_and_its_links(client, team, integration):
    db(client, lambda: GameLink.create(
        integration_id=integration["id"], user_id=team["owner"]["id"],
        player_id="p", player_name="Steve"))

    assert client.delete(f"/api/game-integrations/{integration['id']}").status_code == 204

    assert db(client, GameIntegration.filter(id=integration["id"]).count) == 0
    assert db(client, GameLink.filter(integration_id=integration["id"]).count) == 0
    assert client.delete(f"/api/game-integrations/{integration['id']}").status_code == 404


def test_the_list_counts_linked_players_per_integration(client, team, integration):
    sid = team["server"]["id"]
    second = make_integration(client, sid, team["lobby"]["id"], name="Second")
    for user, player in ((team["owner"], "p1"), (team["member"], "p2")):
        db(client, lambda user=user, player=player: GameLink.create(
            integration_id=integration["id"], user_id=user["id"],
            player_id=player, player_name=player.upper()))

    counts = {i["id"]: i["linked_count"]
              for i in client.get(f"/api/servers/{sid}/game-integrations").json()}
    assert counts == {integration["id"]: 2, second["id"]: 0}


@pytest.fixture
def linked(client, team, integration):
    for user, player in ((team["owner"], "p1"), (team["member"], "p2")):
        db(client, lambda user=user, player=player: GameLink.create(
            integration_id=integration["id"], user_id=user["id"],
            player_id=player, player_name=player.upper()))
    return integration


def test_links_list_the_linked_users(client, team, linked):
    res = client.get(f"/api/game-integrations/{linked['id']}/links")

    assert res.status_code == 200
    assert [(l["user_id"], l["username"], l["player_id"], l["player_name"]) for l in res.json()] == [
        (team["owner"]["id"], team["owner"]["username"], "p1", "P1"),
        (team["member"]["id"], team["member"]["username"], "p2", "P2"),
    ]
    assert all(l["created_at"] for l in res.json())


def test_a_manager_unlinks_anyone(client, team, linked):
    res = team["manager_client"].delete(
        f"/api/game-integrations/{linked['id']}/links/{team['member']['id']}")

    assert res.status_code == 204
    assert [l["player_id"] for l in client.get(
        f"/api/game-integrations/{linked['id']}/links").json()] == ["p1"]


def test_a_player_unlinks_themselves_without_managing(team, linked):
    member = team["member_client"]

    res = member.delete(f"/api/game-integrations/{linked['id']}/links/{team['member']['id']}")

    assert res.status_code == 204
    again = member.delete(f"/api/game-integrations/{linked['id']}/links/{team['member']['id']}")
    assert (again.status_code, again.json()["detail"]) == (404, "Integration not found")


def test_a_player_cannot_unlink_someone_else(client, team, linked):
    res = team["member_client"].delete(
        f"/api/game-integrations/{linked['id']}/links/{team['owner']['id']}")

    assert res.status_code == 404
    assert len(client.get(f"/api/game-integrations/{linked['id']}/links").json()) == 2


def test_a_manager_unlinking_nobody_is_told_so(client, team, linked):
    res = client.delete(f"/api/game-integrations/{linked['id']}/links/{team['manager']['id']}")

    assert (res.status_code, res.json()["detail"]) == (404, "Link not found")


@pytest.mark.parametrize(
    ("app_url_env", "origins", "expected"),
    [
        ("https://zeta.example/", ["http://localhost:5173"], "https://zeta.example"),
        ("", ["http://localhost:5173/", "https://other"], "http://localhost:5173"),
        (" https://zeta.example ", [], "https://zeta.example"),
        ("", [], ""),
    ],
    ids=["env-wins", "first-origin", "trimmed", "nothing-set"],
)
def test_app_url(monkeypatch, app_url_env, origins, expected):
    monkeypatch.setenv("APP_URL", app_url_env)
    monkeypatch.setattr("app.settings.ALLOWED_ORIGINS", origins)

    assert app_url() == expected
