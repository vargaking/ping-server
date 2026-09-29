"""Image and file attachments: upload, authenticated download, sending them
with a message over the socket, edit/delete and pruning."""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.models.Attachment import Attachment
from app.services.attachments import prune_attachments, sanitize_filename, sniff_image
from app.services.chat_service import ChatService
from tests.conftest import ORIGIN, create_channel, create_server, register

HEADERS = {"origin": ORIGIN}
DOC = {"type": "doc", "content": [{"type": "paragraph",
                                   "content": [{"type": "text", "text": "hi"}]}]}
EMPTY_DOC = {"type": "doc", "content": [{"type": "paragraph"}]}
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
HTML = b"<html><script>alert(1)</script></html>"


@pytest.fixture(autouse=True)
def attachments_dir(monkeypatch, tmp_path):
    root = tmp_path / "attachments"
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(root))
    return root


def png_bytes(width=3, height=2) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def files_on_disk(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def invite_and_join(owner_client, joiner_client, server_id):
    invite = owner_client.post("/invites/", json={"server_id": server_id})
    assert invite.status_code == 201, invite.text
    res = joiner_client.post(f"/invites/{invite.json()['id']}/use", json={})
    assert res.status_code == 200, res.text


def upload(client, data=None, *, name="pic.png", content_type="image/png",
           channel_id=None, conversation_id=None):
    form = {}
    if channel_id is not None:
        form["channel_id"] = str(channel_id)
    if conversation_id is not None:
        form["conversation_id"] = str(conversation_id)
    return client.post(
        "/attachments",
        files={"file": (name, png_bytes() if data is None else data, content_type)},
        data=form,
    )


def channel_frame(server_id, channel_id, attachment_ids=(), content=DOC, message_id=None):
    return {
        "type": "message",
        "id": message_id or str(uuid.uuid4()),
        "server_id": server_id,
        "channel_id": channel_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachment_ids": list(attachment_ids),
    }


def dm_frame(conversation_id, attachment_ids=(), content=DOC):
    return {
        "type": "direct_message",
        "id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachment_ids": list(attachment_ids),
    }


def recv(ws, wanted_type):
    """Drain frames (presence noise included) until one of *wanted_type*."""
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


def run(client, func, *args):
    """Run a coroutine function on the app's event loop, where the DB lives."""
    return client.portal.call(func, *args)


@pytest.fixture
def team(client, new_client):
    """alice (owner) and bob in one server/channel, plus outsider erin."""
    alice = register(client, "alice-att")
    server = create_server(client)
    channel = create_channel(client, server["id"])
    bob_client = new_client()
    bob = register(bob_client, "bob-att")
    invite_and_join(client, bob_client, server["id"])
    erin_client = new_client()
    register(erin_client, "erin-att")
    return {
        "alice": client, "alice_user": alice,
        "bob": bob_client, "bob_user": bob,
        "erin": erin_client,
        "server": server, "channel": channel,
    }


def open_conversation(client, other_user_id):
    res = client.post("/conversations/", json={"user_id": other_user_id})
    assert res.status_code == 200, res.text
    return res.json()


# --- unit ---------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("photo.png", "photo.png"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\me\\report.pdf", "report.pdf"),
    ("a\x00b\x1f.txt", "a b .txt"),
    ("  many   spaces \t here.txt ", "many spaces here.txt"),
    ("", "file"),
    ("..", "file"),
    ("dir/", "file"),
    ("x" * 400, "x" * 255),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sniff_image_accepts_only_allowed_formats():
    assert sniff_image(png_bytes(5, 7)) == ("image/png", 5, 7)
    bmp = BytesIO()
    Image.new("RGB", (2, 2)).save(bmp, format="BMP")
    assert sniff_image(bmp.getvalue()) is None
    assert sniff_image(HTML) is None
    assert sniff_image(png_bytes()[:20]) is None


# --- upload -------------------------------------------------------------

def test_upload_image_is_sniffed_not_trusted(team):
    res = upload(team["alice"], png_bytes(5, 7), name="pic.png",
                 content_type="text/html", channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "image"
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (5, 7)
    assert body["filename"] == "pic.png"
    assert body["url"] == f"/attachments/{body['id']}"
    assert body["size"] == len(png_bytes(5, 7))


def test_upload_pdf_is_a_file(team):
    res = upload(team["alice"], PDF, name="doc.pdf", content_type="application/pdf",
                 channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "file"
    assert body["content_type"] == "application/pdf"
    assert body["width"] is None and body["height"] is None


def test_upload_html_is_a_file(team):
    res = upload(team["alice"], HTML, name="page.html", content_type="text/html",
                 channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == "file"


def test_upload_bad_declared_type_falls_back(team):
    res = upload(team["alice"], PDF, name="x.bin", content_type="not a type",
                 channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    assert res.json()["content_type"] == "application/octet-stream"


def test_upload_stores_file_without_user_extension(team, attachments_dir):
    res = upload(team["alice"], PDF, name="evil.html", content_type="text/html",
                 channel_id=team["channel"]["id"])
    assert res.status_code == 201, res.text
    (stored,) = files_on_disk(attachments_dir)
    assert stored.parent == attachments_dir / "channels" / str(team["channel"]["id"])
    assert stored.suffix == ""


def test_upload_too_large(team, monkeypatch):
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "10")
    res = upload(team["alice"], b"x" * 11, name="big.bin", content_type="application/octet-stream",
                 channel_id=team["channel"]["id"])
    assert res.status_code == 413
    assert "10 bytes" in res.json()["detail"]


@pytest.mark.parametrize("size, expected", [
    (10 * 1024 * 1024, "10 MB"),
    (1536 * 1024, "1.5 MB"),
    (2048, "2 KB"),
    (10, "10 bytes"),
])
def test_limit_is_formatted_for_humans(size, expected):
    from app.routers.attachments import _format_limit
    assert _format_limit(size) == expected


def test_upload_empty_file(team):
    res = upload(team["alice"], b"", name="empty.txt", channel_id=team["channel"]["id"])
    assert res.status_code == 422


def test_upload_non_member_forbidden(team):
    res = upload(team["erin"], channel_id=team["channel"]["id"])
    assert res.status_code == 403


def test_upload_unknown_channel(team):
    assert upload(team["alice"], channel_id=99999).status_code == 404


def test_upload_requires_login(new_client, team):
    res = upload(new_client(), channel_id=team["channel"]["id"])
    assert res.status_code == 401


def test_upload_needs_exactly_one_target(team):
    alice = team["alice"]
    convo = open_conversation(alice, team["bob_user"]["id"])
    assert upload(alice).status_code == 422
    both = upload(alice, channel_id=team["channel"]["id"], conversation_id=convo["id"])
    assert both.status_code == 422


def test_upload_to_conversation(team):
    convo = open_conversation(team["alice"], team["bob_user"]["id"])
    res = upload(team["alice"], conversation_id=convo["id"])
    assert res.status_code == 201, res.text
    assert upload(team["bob"], conversation_id=convo["id"]).status_code == 201


def test_upload_to_conversation_non_participant(team):
    convo = open_conversation(team["alice"], team["bob_user"]["id"])
    assert upload(team["erin"], conversation_id=convo["id"]).status_code == 404


# --- download -----------------------------------------------------------

def test_member_downloads_image_inline(team):
    data = png_bytes(4, 4)
    att = upload(team["alice"], data, channel_id=team["channel"]["id"]).json()
    res = team["bob"].get(att["url"])
    assert res.status_code == 200
    assert res.content == data
    assert res.headers["content-type"] == "image/png"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in res.headers["content-security-policy"]
    assert res.headers["content-disposition"] == "inline"
    assert res.headers["cache-control"] == "private, max-age=3600"


def test_member_downloads_file_as_attachment(team):
    att = upload(team["alice"], PDF, name="my report é.pdf", content_type="application/pdf",
                 channel_id=team["channel"]["id"]).json()
    res = team["bob"].get(att["url"])
    assert res.status_code == 200
    assert res.content == PDF
    assert res.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''my%20report%20%C3%A9.pdf")
    assert res.headers["x-content-type-options"] == "nosniff"


def test_html_upload_is_forced_to_download(team):
    att = upload(team["alice"], HTML, name="page.html", content_type="text/html",
                 channel_id=team["channel"]["id"]).json()
    res = team["alice"].get(att["url"])
    assert res.status_code == 200
    assert res.content == HTML
    assert res.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert "sandbox" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_download_hides_attachments_from_outsiders(team):
    att = upload(team["alice"], channel_id=team["channel"]["id"]).json()
    assert team["erin"].get(att["url"]).status_code == 404
    assert team["alice"].get(f"/attachments/{uuid.uuid4()}").status_code == 404
    assert team["alice"].get("/attachments/not-a-uuid").status_code == 404


def test_dm_attachment_only_for_participants(team):
    convo = open_conversation(team["alice"], team["bob_user"]["id"])
    att = upload(team["alice"], conversation_id=convo["id"]).json()
    assert team["bob"].get(att["url"]).status_code == 200
    assert team["erin"].get(att["url"]).status_code == 404


def test_download_requires_login(team, new_client):
    att = upload(team["alice"], channel_id=team["channel"]["id"]).json()
    assert new_client().get(att["url"]).status_code == 401


def test_download_missing_file_is_404(team, attachments_dir):
    att = upload(team["alice"], channel_id=team["channel"]["id"]).json()
    for path in files_on_disk(attachments_dir):
        path.unlink()
    assert team["alice"].get(att["url"]).status_code == 404


def test_attachments_are_not_served_from_media(team, attachments_dir):
    upload(team["alice"], channel_id=team["channel"]["id"])
    (stored,) = files_on_disk(attachments_dir)
    rel = stored.relative_to(attachments_dir).as_posix()
    assert team["alice"].get(f"/media/{rel}").status_code == 404


# --- sending over the socket ---------------------------------------------

def connect_both(alice, bob, stack):
    alice_ws = stack.enter_context(alice.websocket_connect("/ws", headers=HEADERS))
    bob_ws = stack.enter_context(bob.websocket_connect("/ws", headers=HEADERS))
    return alice_ws, bob_ws


@pytest.fixture
def sockets(team):
    from contextlib import ExitStack
    with ExitStack() as stack:
        yield connect_both(team["alice"], team["bob"], stack)


def history(client, channel_id):
    res = client.get(f"/channels/{channel_id}/messages")
    assert res.status_code == 200, res.text
    return res.json()["messages"]


def test_send_message_with_attachment_and_empty_content(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    first = upload(team["alice"], png_bytes(), channel_id=channel_id).json()
    second = upload(team["alice"], PDF, name="a.pdf", content_type="application/pdf",
                    channel_id=channel_id).json()

    frame = channel_frame(server_id, channel_id, [second["id"], first["id"]], content="")
    alice_ws.send_json(frame)

    received = recv(bob_ws, "message")
    assert received["id"] == frame["id"]
    assert received["attachments"] == [second, first]

    (stored,) = history(team["bob"], channel_id)
    assert stored["id"] == frame["id"]
    assert stored["content"] == ""
    assert {a["id"] for a in stored["attachments"]} == {first["id"], second["id"]}


def test_every_message_payload_has_attachments_key(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    alice_ws.send_json(channel_frame(server_id, channel_id))
    received = recv(bob_ws, "message")
    assert received["attachments"] == []
    (stored,) = history(team["bob"], channel_id)
    assert stored["attachments"] == []


def test_history_and_delta_sync_include_attachments(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    with_att = channel_frame(server_id, channel_id, [att["id"]])
    plain = channel_frame(server_id, channel_id)
    alice_ws.send_json(with_att)
    recv(bob_ws, "message")
    alice_ws.send_json(plain)
    recv(bob_ws, "message")

    by_id = {m["id"]: m for m in history(team["bob"], channel_id)}
    assert by_id[with_att["id"]]["attachments"] == [att]
    assert by_id[plain["id"]]["attachments"] == []

    res = team["bob"].get("/channels/messages", params={"last_updated": "2000-01-01T00:00:00"})
    assert res.status_code == 200, res.text
    synced = {m["id"]: m for m in res.json()}
    assert synced[with_att["id"]]["attachments"] == [att]
    assert synced[plain["id"]]["attachments"] == []


def test_attachment_cannot_be_sent_twice(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    alice_ws.send_json(channel_frame(server_id, channel_id, [att["id"]]))
    recv(bob_ws, "message")

    again = channel_frame(server_id, channel_id, [att["id"]])
    alice_ws.send_json(again)
    error = recv(alice_ws, "error")
    assert error["code"] == "invalid_attachments"
    assert error["ref"] == again["id"]
    assert len(history(team["alice"], channel_id)) == 1


def test_duplicate_id_in_one_frame_is_rejected(team, sockets):
    alice_ws, _ = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    alice_ws.send_json(channel_frame(server_id, channel_id, [att["id"], att["id"]]))
    assert recv(alice_ws, "error")["code"] == "invalid_attachments"
    assert history(team["alice"], channel_id) == []


def test_someone_elses_attachment_is_rejected(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    frame = channel_frame(server_id, channel_id, [att["id"]])
    bob_ws.send_json(frame)
    assert recv(bob_ws, "error")["code"] == "invalid_attachments"
    assert history(team["alice"], channel_id) == []


def test_unknown_and_malformed_attachment_ids_are_rejected(team, sockets):
    alice_ws, _ = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        alice_ws.send_json(channel_frame(server_id, channel_id, [bad]))
        assert recv(alice_ws, "error")["code"] == "invalid_attachments"
    assert history(team["alice"], channel_id) == []


def test_attachment_from_another_channel_is_rejected(team, sockets):
    alice_ws, _ = sockets
    server_id = team["server"]["id"]
    other = create_channel(team["alice"], server_id, "other")
    att = upload(team["alice"], channel_id=team["channel"]["id"]).json()
    alice_ws.send_json(channel_frame(server_id, other["id"], [att["id"]]))
    assert recv(alice_ws, "error")["code"] == "invalid_attachments"
    assert history(team["alice"], other["id"]) == []


def test_attachment_taken_concurrently_stores_nothing(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    alice_ws.send_json(channel_frame(server_id, channel_id, [att["id"]]))
    recv(bob_ws, "message")

    async def send_with_stale_copy():
        stale = await Attachment.get(id=att["id"])
        stale.message_id = None
        return await ChatService._create_message(
            [stale], uuid=str(uuid.uuid4()), content="x",
            author_id=team["alice_user"]["id"], server_id=server_id,
            channel_id=channel_id, timestamp=datetime.now(timezone.utc))

    assert run(team["alice"], send_with_stale_copy) is None
    assert len(history(team["alice"], channel_id)) == 1


def test_dm_attachment_cannot_go_to_a_channel(team, sockets):
    alice_ws, _ = sockets
    convo = open_conversation(team["alice"], team["bob_user"]["id"])
    att = upload(team["alice"], conversation_id=convo["id"]).json()
    alice_ws.send_json(
        channel_frame(team["server"]["id"], team["channel"]["id"], [att["id"]]))
    assert recv(alice_ws, "error")["code"] == "invalid_attachments"


def test_more_than_ten_attachments_fail_validation(team, sockets):
    alice_ws, _ = sockets
    frame = channel_frame(team["server"]["id"], team["channel"]["id"],
                          [str(uuid.uuid4()) for _ in range(11)])
    alice_ws.send_json(frame)
    error = recv(alice_ws, "error")
    assert error["code"] == "invalid_frame"
    assert error["ref"] == frame["id"]


def test_mention_only_message_without_attachments_is_stored(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    mention_doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "mention", "attrs": {"id": "1", "label": "bob"}}]}]}
    frame = channel_frame(server_id, channel_id, content=mention_doc)
    alice_ws.send_json(frame)

    received = recv(bob_ws, "message")
    assert received["id"] == frame["id"]
    assert received["content"] == mention_doc
    assert received["attachments"] == []
    (stored,) = history(team["bob"], channel_id)
    assert stored["id"] == frame["id"]


def test_empty_doc_is_allowed_with_an_attachment(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    alice_ws.send_json(channel_frame(server_id, channel_id, [att["id"]], content=EMPTY_DOC))
    assert recv(bob_ws, "message")["attachments"] == [att]


def test_dm_send_with_attachment(team):
    alice, bob = team["alice"], team["bob"]
    convo = open_conversation(alice, team["bob_user"]["id"])
    att = upload(alice, PDF, name="a.pdf", content_type="application/pdf",
                 conversation_id=convo["id"]).json()

    with alice.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        frame = dm_frame(convo["id"], [att["id"]], content="")
        alice_ws.send_json(frame)
        received = recv(bob_ws, "direct_message")
        assert received["attachments"] == [att]

        plain = dm_frame(convo["id"])
        alice_ws.send_json(plain)
        assert recv(bob_ws, "direct_message")["attachments"] == []

        # Already attached, and someone else's.
        alice_ws.send_json(dm_frame(convo["id"], [att["id"]]))
        assert recv(alice_ws, "error")["code"] == "invalid_attachments"
        mine = upload(alice, conversation_id=convo["id"]).json()
        bob_ws.send_json(dm_frame(convo["id"], [mine["id"]]))
        assert recv(bob_ws, "error")["code"] == "invalid_attachments"

    res = alice.get(f"/conversations/{convo['id']}/messages")
    assert res.status_code == 200, res.text
    by_id = {m["id"]: m for m in res.json()["messages"]}
    assert set(by_id) == {frame["id"], plain["id"]}
    assert by_id[frame["id"]]["attachments"] == [att]
    assert by_id[plain["id"]]["attachments"] == []


def test_dm_attachment_from_another_conversation_is_rejected(team):
    alice = team["alice"]
    convo = open_conversation(alice, team["bob_user"]["id"])
    erin = team["erin"].get("/auth/me").json()
    other = open_conversation(alice, erin["id"])
    att = upload(alice, conversation_id=other["id"]).json()
    with alice.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        alice_ws.send_json(dm_frame(convo["id"], [att["id"]]))
        assert recv(alice_ws, "error")["code"] == "invalid_attachments"


# --- edit and delete -----------------------------------------------------

def test_edit_keeps_attachments(team, sockets):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    frame = channel_frame(server_id, channel_id, [att["id"]])
    alice_ws.send_json(frame)
    recv(bob_ws, "message")

    res = team["alice"].patch(f"/messages/{frame['id']}", json={"content": "edited"})
    assert res.status_code == 200, res.text
    assert res.json()["attachments"] == [att]
    assert recv(bob_ws, "message_updated")["attachments"] == [att]


def test_delete_message_removes_files(team, sockets, attachments_dir):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    att = upload(team["alice"], channel_id=channel_id).json()
    frame = channel_frame(server_id, channel_id, [att["id"]])
    alice_ws.send_json(frame)
    recv(bob_ws, "message")
    assert len(files_on_disk(attachments_dir)) == 1

    res = team["alice"].delete(f"/messages/{frame['id']}")
    assert res.status_code == 204
    assert files_on_disk(attachments_dir) == []
    assert team["bob"].get(att["url"]).status_code == 404

    async def count():
        return await Attachment.all().count()

    assert run(team["alice"], count) == 0


# --- pruning ---------------------------------------------------------------

def test_prune_removes_stale_unsent_uploads_and_orphans(team, sockets, attachments_dir):
    alice_ws, bob_ws = sockets
    server_id, channel_id = team["server"]["id"], team["channel"]["id"]
    alice = team["alice"]

    stale = upload(alice, channel_id=channel_id).json()
    fresh = upload(alice, channel_id=channel_id).json()
    sent = upload(alice, channel_id=channel_id).json()
    alice_ws.send_json(channel_frame(server_id, channel_id, [sent["id"]]))
    recv(bob_ws, "message")

    old = datetime.now(timezone.utc) - timedelta(hours=25)

    async def backdate(ids):
        await Attachment.filter(id__in=ids).update(created_at=old)

    run(alice, backdate, [stale["id"], sent["id"]])

    orphan = attachments_dir / "channels" / str(channel_id) / "orphan"
    orphan.write_bytes(b"stray")
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    recent_orphan = attachments_dir / "channels" / str(channel_id) / "recent"
    recent_orphan.write_bytes(b"stray")

    result = run(alice, prune_attachments)
    assert result == {"expired": 1, "orphaned": 1}

    assert alice.get(stale["url"]).status_code == 404
    assert alice.get(fresh["url"]).status_code == 200
    assert alice.get(sent["url"]).status_code == 200
    assert not orphan.exists()
    assert recent_orphan.exists()
    assert len(files_on_disk(attachments_dir)) == 3
