"""ZET-60: server-scoped realtime frames for member-joined / channel-created.

Existing members should see a new channel or a new member without refreshing;
the actor themselves is excluded (they already have the data from their own
request / initial load).
"""
import uuid
from datetime import datetime, timezone

from tests.conftest import ORIGIN, create_channel, create_server, register, ws_ready

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}


def chat_frame(server_id, channel_id):
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": DOC,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def invite_and_join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def test_channel_created_broadcasts_and_excludes_creator(client, new_client):
    register(client, "alice-cc")
    server = create_server(client)
    create_channel(client, server["id"])  # the initial #general
    bob_client = new_client()
    register(bob_client, "bob-cc")
    invite_and_join(client, bob_client, server["id"])

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        new_ch = create_channel(client, server["id"], name="random")

        frame = bob_ws.receive_json()
        assert frame == {
            "type": "channel_created",
            "server_id": server["id"],
            "channel": {
                "id": new_ch["id"],
                "name": "random",
                "channel_settings": {},
                "type": "text",
                "topic": None,
                "group_id": None,
                "position": 1,
                "last_read_message_id": None,
                "last_message_id": None,
                "private": False,
            },
        }

        # The creator is excluded: her next queued frame is bob's chat message,
        # not the channel_created she triggered.
        bob_ws.send_json(chat_frame(server["id"], new_ch["id"]))
        assert alice_ws.receive_json()["type"] == "message"


def test_member_joined_broadcasts_to_existing_members(client, new_client):
    register(client, "alice-mj")
    server = create_server(client)

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        carol_client = new_client()
        carol = register(carol_client, "carol-mj")
        invite_and_join(client, carol_client, server["id"])

        frame = alice_ws.receive_json()
        assert frame["type"] == "member_joined"
        assert frame["server_id"] == server["id"]
        assert frame["member"]["id"] == carol["id"]
        assert frame["member"]["username"] == carol["username"]


def test_creating_a_server_reaches_the_creators_other_tabs(client):
    register(client, "maker-tabs")

    with client.websocket_connect("/ws", headers=HEADERS) as other_tab:
        ws_ready(other_tab)
        server = create_server(client)

        frame = other_tab.receive_json()
        assert frame["type"] == "server_added"
        assert frame["server"]["id"] == server["id"]
        assert frame["server"]["permissions"] != "0"


def test_joining_a_server_reaches_the_joiners_other_tabs(client, new_client):
    register(client, "owner-tabs")
    server = create_server(client)
    joiner_client = new_client()
    register(joiner_client, "joiner-tabs")

    with joiner_client.websocket_connect("/ws", headers=HEADERS) as other_tab:
        ws_ready(other_tab)
        invite_and_join(client, joiner_client, server["id"])

        frame = other_tab.receive_json()
        assert frame["type"] == "server_added"
        assert frame["server"]["id"] == server["id"]
        assert frame["server"]["permissions"] != "0"
