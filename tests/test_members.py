"""Server members: listing, kicking and leaving."""
from app.services.voice_presence import VoicePresence
from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready
from tests.test_realtime_events import chat_frame, invite_and_join

HEADERS = {"origin": ORIGIN}


def setup_server(client, new_client, *usernames):
    owner = register(client, "owner-" + usernames[0])
    server = create_server(client)
    members = []
    for name in usernames:
        c = new_client()
        user = register(c, name)
        invite_and_join(client, c, server["id"])
        members.append((c, user))
    return owner, server, members


def test_list_members_returns_owner_first_with_join_dates(client, new_client):
    owner, server, [(bob_client, bob)] = setup_server(client, new_client, "bob-ls")

    res = bob_client.get(f"/servers/{server['id']}/members")
    assert res.status_code == 200
    body = res.json()
    assert [m["user"]["id"] for m in body] == [owner["id"], bob["id"]]
    assert [m["is_owner"] for m in body] == [True, False]
    assert all(m["joined_at"] for m in body)


def test_list_members_hides_server_from_non_members(client, new_client):
    register(client, "owner-hide")
    server = create_server(client)
    outsider = new_client()
    register(outsider, "outsider-hide")

    assert outsider.get(f"/servers/{server['id']}/members").status_code == 404
    assert outsider.get("/servers/999999/members").status_code == 404


def test_owner_kicks_member_and_everyone_is_told(client, new_client):
    owner, server, [(bob_client, bob), (carol_client, carol)] = setup_server(
        client, new_client, "bob-kick", "carol-kick")

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        ws_ready(bob_ws)
        ws_ready(carol_ws)
        bob_ws.receive_json()    # carol online

        res = client.delete(f"/servers/{server['id']}/members/{bob['id']}")
        assert res.status_code == 204

        expected = {
            "type": "member_left",
            "server_id": server["id"],
            "user_id": bob["id"],
            "reason": "kicked",
        }
        assert bob_ws.receive_json() == expected
        assert carol_ws.receive_json() == expected

    assert bob_client.get(f"/servers/{server['id']}").status_code == 404
    ids = [m["user"]["id"] for m in client.get(f"/servers/{server['id']}/members").json()]
    assert ids == [owner["id"], carol["id"]]


def test_non_owner_cannot_kick(client, new_client):
    owner, server, [(bob_client, _), (_, carol)] = setup_server(
        client, new_client, "bob-nk", "carol-nk")

    res = bob_client.delete(f"/servers/{server['id']}/members/{carol['id']}")
    assert res.status_code == 403
    res = bob_client.delete(f"/servers/{server['id']}/members/{owner['id']}")
    assert res.status_code == 403
    assert len(client.get(f"/servers/{server['id']}/members").json()) == 3


def test_owner_cannot_leave_or_kick_themselves(client, new_client):
    owner, server, _ = setup_server(client, new_client, "bob-ol")

    res = client.delete(f"/servers/{server['id']}/members/{owner['id']}")
    assert res.status_code == 409
    assert client.get(f"/servers/{server['id']}").status_code == 200


def test_kicking_a_non_member_is_404(client, new_client):
    _, server, _ = setup_server(client, new_client, "bob-nm")
    stranger = new_client()
    other = register(stranger, "stranger-nm")

    assert client.delete(f"/servers/{server['id']}/members/{other['id']}").status_code == 404


def test_member_leaves_and_their_other_tabs_hear_it(client, new_client):
    _, server, [(bob_client, bob)] = setup_server(client, new_client, "bob-leave")

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            client.websocket_connect("/ws", headers=HEADERS) as owner_ws:
        ws_ready(bob_ws)
        ws_ready(owner_ws)
        bob_ws.receive_json()    # owner online

        res = bob_client.delete(f"/servers/{server['id']}/members/{bob['id']}")
        assert res.status_code == 204

        expected = {
            "type": "member_left",
            "server_id": server["id"],
            "user_id": bob["id"],
            "reason": "left",
        }
        assert bob_ws.receive_json() == expected
        assert owner_ws.receive_json() == expected

    assert bob_client.get(f"/servers/{server['id']}").status_code == 404


def test_removed_member_stops_receiving_server_messages(client, new_client):
    _, server, [(bob_client, bob), (carol_client, _)] = setup_server(
        client, new_client, "bob-msg", "carol-msg")
    channel = create_channel(client, server["id"])
    client.delete(f"/servers/{server['id']}/members/{bob['id']}")

    with bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws, \
            carol_client.websocket_connect("/ws", headers=HEADERS) as carol_ws:
        ws_ready(bob_ws)
        ws_ready(carol_ws)
        with client.websocket_connect("/ws", headers=HEADERS) as owner_ws:
            ws_ready(owner_ws)
            assert carol_ws.receive_json()["type"] == "presence_update"
            owner_ws.send_json(chat_frame(server["id"], channel["id"]))
            assert carol_ws.receive_json()["type"] == "message"

        # Bob's next frame is the rejection of his own post, so the owner's
        # message never reached him.
        bob_ws.send_json(chat_frame(server["id"], channel["id"]))
        assert bob_ws.receive_json()["code"] == "forbidden"


class VoiceSource:
    def __init__(self, occupied):
        self.occupied = occupied
        self.removed = []
        self.deleted = []

    async def fetch(self):
        return {}

    async def fetch_channel(self, channel_id):
        return ()

    async def remove_participant(self, channel_id, user_id):
        if (channel_id, user_id) not in self.occupied:
            raise RuntimeError("participant not found")
        self.removed.append((channel_id, user_id))

    async def delete_room(self, channel_id):
        self.deleted.append(channel_id)

    async def aclose(self):
        pass


def use_voice_source(client, monkeypatch):
    source = VoiceSource(set())

    async def notify(channel_id, participants):
        pass

    monkeypatch.setattr(client.app.state, "voice_presence", VoicePresence(source, notify))
    return source


def test_kick_disconnects_the_member_from_voice(client, new_client, monkeypatch):
    _, server, [(_, bob)] = setup_server(client, new_client, "bob-voice")
    lounge = create_channel(client, server["id"], name="lounge", channel_type="voice")
    create_channel(client, server["id"], name="games", channel_type="voice")

    source = VoiceSource({(lounge["id"], bob["id"])})

    async def notify(channel_id, participants):
        pass

    monkeypatch.setattr(client.app.state, "voice_presence", VoicePresence(source, notify))

    assert client.delete(f"/servers/{server['id']}/members/{bob['id']}").status_code == 204
    assert source.removed == [(lounge["id"], bob["id"])]


def test_deleting_a_server_closes_the_rooms_of_its_voice_channels(client, monkeypatch):
    register(client, "owner-delete-server")
    server = create_server(client)
    lounge = create_channel(client, server["id"], name="lounge", channel_type="voice")
    games = create_channel(client, server["id"], name="games", channel_type="voice")
    create_channel(client, server["id"], name="chat")
    source = use_voice_source(client, monkeypatch)

    assert client.delete(f"/servers/{server['id']}").status_code == 204
    assert sorted(source.deleted) == sorted([lounge["id"], games["id"]])


def test_deleting_a_voice_channel_closes_its_room(client, monkeypatch):
    register(client, "owner-delete-voice")
    server = create_server(client)
    lounge = create_channel(client, server["id"], name="lounge", channel_type="voice")
    source = use_voice_source(client, monkeypatch)

    assert client.delete(f"/channels/{lounge['id']}").status_code == 204
    assert source.deleted == [lounge["id"]]


def test_deleting_a_text_channel_closes_no_room(client, monkeypatch):
    register(client, "owner-delete-text")
    server = create_server(client)
    chat = create_channel(client, server["id"], name="chat")
    source = use_voice_source(client, monkeypatch)

    assert client.delete(f"/channels/{chat['id']}").status_code == 204
    assert source.deleted == []
