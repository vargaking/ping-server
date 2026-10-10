"""Importing an export from the server settings: upload, check, map authors,
import, re-map. Drives the HTTP API; the work itself is the bundle importer's."""
import io
import json
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.app import app
from app.models.Attachment import Attachment
from app.models.Channel import Channel
from app.models.ChannelGroup import ChannelGroup
from app.models.ForumPost import ForumPost
from app.models.ForumTag import ForumTag
from app.models.Message import Message
from app.models.PermissionOverwrite import PermissionOverwrite
from app.models.Role import Role
from app.models.Server import Server
from app.models.ServerImport import ServerImport
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.permissions import Permission
from app.routers.server_imports import ATTACHMENT_SPACE_MARGIN
from app.services.bundle import importer
from app.services.bundle.importer import ImportAborted, ImportOptions, import_bundle, message_uuid
from app.services.bundle.plan import plan_json
from app.services.imports import runner as runner_module
from app.services.imports.runner import ImportRunner
from app.services.system_user import IMPORTED_PASSWORD_HASH, IMPORTED_USERNAME
from tests.conftest import ORIGIN, create_server, register, ws_ready
from tests.test_channel_permissions import chat_frame
from tests.test_permissions import join

FIXTURE = Path(__file__).parent / "fixtures" / "bundle"
HEADERS = {"origin": ORIGIN}
SOURCE = "discord:9000"
OCTETS = {"content-type": "application/octet-stream"}


def run(client, awaitable):
    async def wait():
        return await awaitable
    return client.portal.call(wait)


@pytest.fixture(autouse=True)
def roots(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    monkeypatch.setenv("IMPORTS_ROOT", str(tmp_path / "imports"))
    monkeypatch.setenv("IMPORT_CHUNK_BYTES", "2048")
    return SimpleNamespace(imports=tmp_path / "imports")


@pytest.fixture
def world(client, new_client):
    """A server with its owner and two members, and carol, who is outside it."""
    owner = register(client, "import-owner")
    server = create_server(client)
    alice_client, alice = join(client, new_client, server["id"])
    bob_client, bob = join(client, new_client, server["id"])
    carol_client = new_client()
    carol = register(carol_client, "import-carol")
    return SimpleNamespace(
        owner=client, owner_user=owner, sid=server["id"], alice=alice_client, alice_user=alice,
        bob=bob_client, bob_user=bob, carol=carol_client, carol_user=carol)


def runner() -> ImportRunner:
    return app.state.import_runner


def zip_of(source: Path = FIXTURE, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, prefix + path.relative_to(source).as_posix())
    return buffer.getvalue()


def zip_of_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def url(sid, *parts) -> str:
    return f"/servers/{sid}/import" + "".join(f"/{part}" for part in parts)


def create(client, sid, size, filename="export.zip"):
    return client.post(url(sid), json={"filename": filename, "size": size})


def put_piece(client, sid, import_id, offset, data):
    return client.put(
        url(sid, import_id, "data"), params={"offset": offset}, content=data, headers=OCTETS)


def upload(client, sid, data: bytes, filename="export.zip") -> dict:
    created = create(client, sid, len(data), filename)
    assert created.status_code == 201, created.text
    import_id = created.json()["id"]
    chunk = client.get(url(sid)).json()["limits"]["chunk_bytes"]
    for offset in range(0, len(data), chunk):
        res = put_piece(client, sid, import_id, offset, data[offset:offset + chunk])
        assert res.status_code == 200, res.text
    return res.json() | {"id": import_id}


def current(client, sid) -> dict:
    return client.get(url(sid)).json()["import"]


def settle(client, sid, import_id) -> dict:
    client.portal.call(runner().wait, UUID(import_id))
    return current(client, sid)


def ready_import(client, sid, data: bytes | None = None) -> dict:
    uploaded = upload(client, sid, data or zip_of())
    got = settle(client, sid, uploaded["id"])
    assert got["status"] == "ready", got
    return got


def set_authors(client, sid, import_id, mapping):
    return client.put(url(sid, import_id, "authors"), json={"authors": mapping})


def start(client, sid, import_id, body=None):
    return client.post(url(sid, import_id, "start"), **({} if body is None else {"json": body}))


def done_import(world, mapping=None, data: bytes | None = None) -> dict:
    got = ready_import(world.owner, world.sid, data)
    res = set_authors(world.owner, world.sid, got["id"], mapping or {})
    assert res.status_code == 200, res.text
    assert start(world.owner, world.sid, got["id"]).status_code == 202
    got = settle(world.owner, world.sid, got["id"])
    assert got["status"] == "done", got
    return got


def counts(client, sid) -> list[int]:
    return [
        run(client, Channel.filter(server_id=sid).count()),
        run(client, ChannelGroup.filter(server_id=sid).count()),
        run(client, Message.filter(server_id=sid).count()),
        run(client, Attachment.filter(server_id=sid).count()),
        run(client, ForumPost.filter(channel__server_id=sid).count()),
        run(client, ForumTag.filter(channel__server_id=sid).count()),
    ]


def message(client, source_id, sid=None) -> Message:
    if sid is None:
        sid = run(client, Server.all().order_by("id").first()).id
    return run(client, Message.get(uuid=message_uuid(sid, SOURCE, source_id)))


def force(client, import_id, **fields):
    run(client, ServerImport.filter(id=import_id).update(**fields))


def import_row(client, import_id) -> ServerImport | None:
    return run(client, ServerImport.get_or_none(id=import_id))


def folder(roots, import_id) -> Path:
    return roots.imports / import_id


def authors_by_id(got) -> dict:
    return {a["id"]: a for a in got["authors"]}


# Access

def test_only_the_owner_can_use_the_routes(client, world, new_client):
    imp = create(client, world.sid, 100).json()
    routes = [
        ("GET", url(world.sid), {}),
        ("POST", url(world.sid), {"json": {"filename": "a.zip", "size": 5}}),
        ("PUT", url(world.sid, imp["id"], "data") + "?offset=0", {"content": b"x"}),
        ("PUT", url(world.sid, imp["id"], "authors"), {"json": {"authors": {}}}),
        ("POST", url(world.sid, imp["id"], "start"), {}),
        ("DELETE", url(world.sid, imp["id"]), {}),
    ]

    for who in (world.alice, world.carol):
        for method, path, kwargs in routes:
            res = who.request(method, path, **kwargs)
            assert (res.status_code, res.json()["detail"]) == (
                403, "Only the server owner can do this"), (method, path)
    assert client.put(url(999999, imp["id"], "authors"), json={"authors": {}}).status_code == 404
    assert client.get(url(999999)).status_code == 404
    assert current(client, world.sid)["received"] == 0


def test_an_unknown_import_is_a_404(client, world):
    for path in (uuid.uuid4(), "not-an-id"):
        assert client.delete(url(world.sid, path)).status_code == 404
        assert start(client, world.sid, path).status_code == 404
        assert put_piece(client, world.sid, path, 0, b"x").status_code == 404
        assert set_authors(client, world.sid, path, {}).status_code == 404


def test_an_import_of_another_server_is_a_404(client, world):
    other = create_server(client, "Other")
    imp = create(client, other["id"], 100).json()

    assert client.delete(url(world.sid, imp["id"])).status_code == 404
    assert current(client, other["id"])["id"] == imp["id"]


def test_get_without_an_import(client, world):
    assert client.get(url(world.sid)).json() == {
        "limits": {"max_bytes": 5 * 1024**3, "chunk_bytes": 2048}, "import": None}


# Creating

@pytest.mark.parametrize("body", [
    {"filename": "export.zip", "size": 0},
    {"filename": "export.zip", "size": -5},
    {"filename": "export.zip"},
    {"size": 5},
    {"filename": "notes.txt", "size": 5},
    {"filename": "export.zip.txt", "size": 5},
    {"filename": "", "size": 5},
    {"filename": "folder/", "size": 5},
    {"filename": "export.zip", "size": "big"},
])
def test_create_validates_the_request(client, world, body):
    assert client.post(url(world.sid), json=body).status_code == 422
    assert current(client, world.sid) is None


def test_create_starts_an_upload_with_a_clean_file_name(client, world):
    first = create(client, world.sid, 1234, "Export.ZIP")
    assert first.status_code == 201
    assert first.json()["filename"] == "Export.ZIP"
    assert (first.json()["status"], first.json()["size"], first.json()["received"]) == (
        "uploading", 1234, 0)
    assert first.json()["source"] is None and first.json()["plan"] is None
    assert first.json()["authors"] == []

    named = create(client, world.sid, 5, "C:\\dumps\\my export.zip")
    assert named.json()["filename"] == "my export.zip"
    assert create(client, world.sid, 5, ("a" * 300) + ".zip").status_code == 422


def test_a_file_over_the_limit_is_refused(client, world, monkeypatch):
    monkeypatch.setenv("IMPORT_MAX_BYTES", "1000")

    refused = create(client, world.sid, 1001)
    assert (refused.status_code, refused.json()["detail"]) == (
        413, "The file is larger than the limit (1000 bytes)")
    assert create(client, world.sid, 1000).status_code == 201
    assert client.get(url(world.sid)).json()["limits"]["max_bytes"] == 1000


def test_with_a_limit_of_zero_imports_are_off_but_leftovers_can_be_deleted(
        client, world, monkeypatch, roots):
    imp = create(client, world.sid, 100).json()
    put_piece(client, world.sid, imp["id"], 0, b"x" * 10)
    monkeypatch.setenv("IMPORT_MAX_BYTES", "0")
    off = "Imports are turned off on this server"

    assert client.get(url(world.sid)).json()["limits"]["max_bytes"] == 0
    for res in (
        create(client, world.sid, 100),
        put_piece(client, world.sid, imp["id"], 0, b"x"),
        set_authors(client, world.sid, imp["id"], {}),
        start(client, world.sid, imp["id"]),
    ):
        assert (res.status_code, res.json()["detail"]) == (403, off)
    assert client.delete(url(world.sid, imp["id"])).status_code == 204
    assert not folder(roots, imp["id"]).exists()


def test_a_new_upload_replaces_unfinished_ones_but_not_a_running_import(client, world, roots):
    first = create(client, world.sid, 100).json()
    assert put_piece(client, world.sid, first["id"], 0, b"x" * 10).status_code == 200
    assert folder(roots, first["id"]).exists()

    second = create(client, world.sid, 100).json()
    assert import_row(client, first["id"]) is None and not folder(roots, first["id"]).exists()
    assert current(client, world.sid)["id"] == second["id"]

    force(client, second["id"], status="importing")
    busy = create(client, world.sid, 100)
    assert (busy.status_code, busy.json()["detail"]) == (409, "An import is already running")
    assert import_row(client, second["id"]) is not None


# Uploading

def test_an_upload_arrives_in_pieces(client, world, roots):
    data = zip_of()
    assert len(data) > 2 * 2048
    imp = create(client, world.sid, len(data)).json()

    first = put_piece(client, world.sid, imp["id"], 0, data[:2048])
    assert first.json() == {"received": 2048, "status": "uploading"}
    assert current(client, world.sid)["received"] == 2048
    second = put_piece(client, world.sid, imp["id"], 2048, data[2048:4096])
    assert second.json() == {"received": 4096, "status": "uploading"}
    last = put_piece(client, world.sid, imp["id"], 4096, data[4096:])
    assert last.json() == {"received": len(data), "status": "unpacking"}
    assert current(client, world.sid)["status"] in ("unpacking", "ready")
    assert settle(client, world.sid, imp["id"])["status"] == "ready"


def test_a_wrong_offset_is_refused_and_the_upload_resumes_from_received(client, world):
    data = zip_of()
    imp = create(client, world.sid, len(data)).json()

    early = put_piece(client, world.sid, imp["id"], 5, data[5:100])
    assert (early.status_code, early.json()["detail"]) == (409, "Wrong offset")
    assert put_piece(client, world.sid, imp["id"], 0, data[:2048]).status_code == 200
    replay = put_piece(client, world.sid, imp["id"], 0, data[:2048])
    assert (replay.status_code, replay.json()["detail"]) == (409, "Wrong offset")
    ahead = put_piece(client, world.sid, imp["id"], 3000, data[3000:4000])
    assert ahead.status_code == 409

    received = current(client, world.sid)["received"]
    assert received == 2048
    assert put_piece(client, world.sid, imp["id"], received, data[received:received + 2048]).status_code == 200
    assert put_piece(client, world.sid, imp["id"], 4096, data[4096:]).json()["status"] == "unpacking"
    assert settle(client, world.sid, imp["id"])["status"] == "ready"


def test_a_piece_over_the_chunk_limit_is_refused(client, world):
    imp = create(client, world.sid, 100_000).json()

    sized = put_piece(client, world.sid, imp["id"], 0, b"x" * 2049)
    assert sized.status_code == 413
    streamed = put_piece(client, world.sid, imp["id"], 0, iter([b"x" * 1500, b"x" * 1500]))
    assert streamed.status_code == 413
    assert current(client, world.sid)["received"] == 0
    assert put_piece(client, world.sid, imp["id"], 0, b"x" * 2048).status_code == 200


def test_a_piece_past_the_declared_size_is_refused(client, world):
    imp = create(client, world.sid, 10).json()

    assert put_piece(client, world.sid, imp["id"], 0, b"x" * 11).status_code == 413
    assert put_piece(client, world.sid, imp["id"], 0, b"x" * 6).status_code == 200
    assert put_piece(client, world.sid, imp["id"], 6, b"x" * 5).status_code == 413
    assert current(client, world.sid)["received"] == 6


def test_an_empty_piece_and_a_negative_offset_are_invalid(client, world):
    imp = create(client, world.sid, 10).json()

    assert put_piece(client, world.sid, imp["id"], 0, b"").status_code == 422
    assert put_piece(client, world.sid, imp["id"], -1, b"x").status_code == 422
    assert client.put(url(world.sid, imp["id"], "data"), content=b"x").status_code == 422


def test_pieces_are_only_taken_while_uploading(client, world):
    got = ready_import(client, world.sid)

    res = put_piece(client, world.sid, got["id"], got["size"], b"x")
    assert (res.status_code, res.json()["detail"]) == (409, "This import is not waiting for data")


# Unpacking and checking

def test_a_finished_upload_is_unpacked_and_checked(client, world, roots):
    before = counts(client, world.sid)
    users_before = run(client, User.all().count())
    data = zip_of()

    got = ready_import(client, world.sid, data)

    direct = plan_json(run(client, import_bundle(
        FIXTURE, ImportOptions(server_id=world.sid, dry_run=True))))
    plan = got["plan"]
    assert plan.pop("free_bytes") > 0
    direct.pop("free_bytes")
    staff = next(c for c in plan["channels"] if c["source_id"] == "103")
    plan["channels"] = [c for c in plan["channels"] if c is not staff]
    direct["channels"] = [c for c in direct["channels"] if c["source_id"] != "103"]
    assert plan == direct
    assert got["source"] == {"platform": "discord", "server_name": "Sample Guild"}
    assert (got["filename"], got["size"], got["received"]) == ("export.zip", len(data), len(data))
    assert got["progress"] is None and got["error"] is None and got["failed_step"] is None
    assert got["result"] is None
    assert counts(client, world.sid) == before
    assert run(client, User.all().count()) == users_before
    assert not (folder(roots, got["id"]) / "upload.zip").exists()
    assert (folder(roots, got["id"]) / "bundle" / "server.json").is_file()
    assert (folder(roots, got["id"]) / "bundle" / "files" / "9001" / "cat.png").is_file()
    assert not (folder(roots, got["id"]) / "bundle" / "avatars").exists()


def test_the_authors_of_the_dry_run_are_listed_with_their_mapping(client, world):
    got = ready_import(client, world.sid)

    assert [(a["id"], a["name"], a["user_id"]) for a in got["authors"]][0][2] is None
    assert sorted(authors_by_id(got)) == ["a1", "a2", "a3", "a4"]
    assert [(a["messages"], a["name"]) for a in got["authors"]] == sorted(
        [(a["messages"], a["name"]) for a in got["authors"]], key=lambda a: (-a[0], a[1]))
    assert all(a["user_id"] is None for a in got["authors"])
    assert "authors" not in got["plan"] and "seen" not in got["plan"]


def test_a_bundle_one_folder_deep_works_too(client, world):
    got = ready_import(client, world.sid, zip_of(FIXTURE, "Sample Guild/"))

    assert got["plan"]["totals"]["messages"] == 13


def test_a_zip_that_is_not_an_export_fails_with_a_reason(client, world, roots):
    cases = [
        (b"this is not a zip", "This file is not a zip archive"),
        (zip_of_files({"readme.txt": b"hi"}), "This zip has no server.json"),
        (zip_of_files({"server.json": b'{"format": 2, "source": {}}'}),
         "Unsupported bundle format 2; this importer reads format 1"),
    ]
    for data, message_text in cases:
        uploaded = upload(client, world.sid, data)
        got = settle(client, world.sid, uploaded["id"])

        assert (got["status"], got["failed_step"], got["error"]) == (
            "failed", "unpacking", message_text)
        assert got["progress"] is None and got["plan"] is None
        assert not folder(roots, uploaded["id"]).exists()
        refused = start(client, world.sid, uploaded["id"])
        assert (refused.status_code, refused.json()["detail"]) == (
            409, "The import isn't ready to start")
        assert set_authors(client, world.sid, uploaded["id"], {}).status_code == 409
        assert client.delete(url(world.sid, uploaded["id"])).status_code == 204


def test_an_export_over_the_unpacked_limit_fails(client, world, monkeypatch):
    monkeypatch.setenv("IMPORT_MAX_UNPACKED_BYTES", "1000")

    uploaded = upload(client, world.sid, zip_of())
    got = settle(client, world.sid, uploaded["id"])

    assert got["status"] == "failed"
    assert got["error"] == "This export is larger than the allowed unpacked size (1000 bytes)"


def test_attachments_over_the_size_limit_are_left_out_and_counted(client, world, monkeypatch, roots):
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "50")

    got = ready_import(client, world.sid)

    assert got["plan"]["over_cap"] >= 1
    assert not (folder(roots, got["id"]) / "bundle" / "files" / "9001" / "cat.png").exists()


def test_a_broken_data_file_is_reported_without_server_paths(client, world, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(FIXTURE, broken)
    (broken / "channels/101/messages/1.json").write_text("{not json")

    got = settle(client, world.sid, upload(client, world.sid, zip_of(broken))["id"])

    assert got["status"] == "failed" and got["failed_step"] == "unpacking"
    assert got["error"].startswith("channels/101/messages/1.json can't be read")
    assert str(tmp_path) not in got["error"]


def test_an_unexpected_error_in_the_check_is_not_shown(client, world, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("secret detail at /srv/imports")

    monkeypatch.setattr(runner_module, "import_bundle", explode)

    got = settle(client, world.sid, upload(client, world.sid, zip_of())["id"])

    assert (got["status"], got["failed_step"], got["error"]) == (
        "failed", "unpacking", "The import failed unexpectedly")


# Authors

def test_authors_are_validated_and_saved_while_ready(client, world):
    got = ready_import(client, world.sid)
    path = lambda mapping: set_authors(client, world.sid, got["id"], mapping)  # noqa: E731
    system = run(client, User.create(
        username=IMPORTED_USERNAME, password_hash=IMPORTED_PASSWORD_HASH, profile={}))

    unknown = path({"zzz": world.alice_user["id"]})
    assert (unknown.status_code, unknown.json()["detail"]) == (
        422, "zzz is not an author of this import")
    outsider = path({"a1": world.carol_user["id"]})
    assert (outsider.status_code, outsider.json()["detail"]) == (
        422, f"User {world.carol_user['id']} is not a member of this server")
    account = path({"a1": system.id})
    assert (account.status_code, account.json()["detail"]) == (
        422, "The import account can't be mapped to an author")
    assert path({"a1": "alice"}).status_code == 422
    assert path({"a1": 999999}).status_code == 422
    assert all(a["user_id"] is None for a in current(client, world.sid)["authors"])

    saved = path({"a1": world.alice_user["id"], "a2": world.bob_user["id"], "a3": None})
    assert saved.status_code == 200
    assert saved.json()["status"] == "ready"
    mapped = {a["id"]: a["user_id"] for a in saved.json()["authors"]}
    assert mapped == {
        "a1": world.alice_user["id"], "a2": world.bob_user["id"], "a3": None, "a4": None}
    assert current(client, world.sid)["authors"] == saved.json()["authors"]
    assert path({"a1": world.owner_user["id"]}).status_code == 200
    assert {a["id"]: a["user_id"] for a in current(client, world.sid)["authors"]}["a2"] is None


def test_authors_can_only_change_when_ready_or_done(client, world):
    uploading = create(client, world.sid, 100).json()

    res = set_authors(client, world.sid, uploading["id"], {})
    assert (res.status_code, res.json()["detail"]) == (
        409, "The import can't be changed right now")
    force(client, uploading["id"], status="importing")
    assert set_authors(client, world.sid, uploading["id"], {}).status_code == 409


# Importing

def snapshot(client, sid) -> dict:
    async def collect():
        users = {u.id: u.username for u in await User.all()}
        channels = {c.id: c.name for c in await Channel.filter(server_id=sid)}
        messages = await Message.filter(server_id=sid)
        return {
            "channels": sorted(
                (c.name, c.type, c.topic, c.group_id is not None)
                for c in await Channel.filter(server_id=sid)),
            "groups": sorted(g.name for g in await ChannelGroup.filter(server_id=sid)),
            "messages": sorted(
                (channels[m.channel_id], m.metadata["import"]["id"], users[m.author_id],
                 m.metadata.get("imported_author"), m.content, m.reply_to_uuid is not None,
                 m.post_id is not None, m.timestamp)
                for m in messages),
            "attachments": sorted(
                (a.filename, a.size, a.kind, a.content_type)
                for a in await Attachment.filter(server_id=sid)),
            "posts": sorted(
                (p.title, p.pinned, p.locked, p.reply_count, users[p.author_id])
                for p in await ForumPost.filter(channel__server_id=sid)),
            "tags": sorted(t.name for t in await ForumTag.filter(channel__server_id=sid)),
        }
    return run(client, collect())


def test_the_import_gives_what_a_direct_import_gives(client, world, tmp_path, roots):
    mapping = {"a1": world.alice_user["id"], "a2": world.bob_user["id"]}
    got = ready_import(client, world.sid)
    assert set_authors(client, world.sid, got["id"], mapping).status_code == 200

    started = start(client, world.sid, got["id"])
    assert started.status_code == 202
    assert started.json()["plan"] == got["plan"]
    assert started.json()["status"] == "importing"
    assert started.json()["progress"] == {
        "phase": "importing", "done": 0, "total": 13, "label": None}
    done = settle(client, world.sid, got["id"])

    assert done["status"] == "done" and done["progress"] is None and done["error"] is None
    assert done["result"]["totals"]["messages"] == 13 and done["result"]["totals"]["posts"] == 2
    assert done["plan"] == got["plan"]
    assert {a["id"]: a["user_id"] for a in done["authors"]} == {
        "a1": mapping["a1"], "a2": mapping["a2"], "a3": None, "a4": None}

    twin = tmp_path / "twin"
    shutil.copytree(FIXTURE, twin)
    data = json.loads((twin / "server.json").read_text())
    data["source"]["server_id"] = "9001"
    (twin / "server.json").write_text(json.dumps(data))
    other = create_server(client, "Direct")
    run(client, import_bundle(twin, ImportOptions(server_id=other["id"], authors={
        "a1": world.alice_user["username"], "a2": world.bob_user["username"]})))
    assert snapshot(client, world.sid) == snapshot(client, other["id"])
    assert counts(client, world.sid) == counts(client, other["id"])
    assert counts(client, world.sid)[2] == 13

    bundle = folder(roots, got["id"]) / "bundle"
    assert not (bundle / "files").exists()
    assert not (folder(roots, got["id"]) / "upload.zip").exists()
    assert (bundle / "server.json").is_file()
    assert (bundle / "channels" / "101" / "messages" / "1.json").is_file()


def test_an_upload_cannot_be_started(client, world):
    uploading = create(client, world.sid, 100).json()

    refused = start(client, world.sid, uploading["id"])

    assert (refused.status_code, refused.json()["detail"]) == (
        409, "The import isn't ready to start")


def test_start_is_refused_when_the_attachments_will_not_fit(client, world, monkeypatch):
    got = ready_import(client, world.sid)
    assert got["plan"]["totals"]["attachment_bytes"] > 0

    with monkeypatch.context() as patched:
        patched.setattr(
            shutil, "disk_usage", lambda path: SimpleNamespace(total=10, used=10, free=1000))
        refused = start(client, world.sid, got["id"])
    assert (refused.status_code, refused.json()["detail"]) == (
        409, "Not enough disk space for the attachments")
    assert current(client, world.sid)["status"] == "ready"
    assert start(client, world.sid, got["id"]).status_code == 202
    assert settle(client, world.sid, got["id"])["status"] == "done"


def test_a_failed_import_starts_again_where_it_stopped(client, world, monkeypatch):
    got = ready_import(client, world.sid)
    real = runner_module.import_bundle
    calls = []

    async def fails_once(bundle, options, progress=None):
        calls.append(options)
        if len(calls) == 1:
            raise RuntimeError("disk exploded at /srv/imports/secret")
        return await real(bundle, options, progress)

    monkeypatch.setattr(runner_module, "import_bundle", fails_once)
    assert start(client, world.sid, got["id"]).status_code == 202
    failed = settle(client, world.sid, got["id"])

    assert (failed["status"], failed["failed_step"], failed["error"]) == (
        "failed", "importing", "The import failed unexpectedly")
    assert failed["progress"] is None and failed["result"] is None
    assert set_authors(client, world.sid, got["id"], {}).status_code == 409

    assert start(client, world.sid, got["id"]).status_code == 202
    done = settle(client, world.sid, got["id"])
    assert done["status"] == "done" and done["error"] is None and done["failed_step"] is None
    assert counts(client, world.sid)[2] == 13
    assert calls[1].private == {} and calls[1].existing_only is False


def test_an_import_that_aborts_says_why(client, world, monkeypatch):
    got = ready_import(client, world.sid)

    async def aborts(*args, **kwargs):
        raise ImportAborted("The import would create 9999 channels")

    monkeypatch.setattr(runner_module, "import_bundle", aborts)
    start(client, world.sid, got["id"])
    failed = settle(client, world.sid, got["id"])

    assert failed["error"] == "The import would create 9999 channels"


def test_private_channels_are_not_imported(client, world):
    done = done_import(world)

    assert not run(client, Channel.filter(server_id=world.sid, name="staff").exists())
    assert done["result"]["left_out"]["private_channels"] == 1
    skipped = {c["name"]: c for c in done["result"]["channels"]}["staff"]
    assert (skipped["action"], skipped["reason"]) == ("skipped", "private")


def test_the_mapping_drops_users_that_no_longer_exist(client, world):
    got = ready_import(client, world.sid)
    set_authors(client, world.sid, got["id"], {
        "a1": world.alice_user["id"], "a2": world.bob_user["id"]})
    run(client, User.filter(id=world.bob_user["id"]).delete())

    start(client, world.sid, got["id"])
    done = settle(client, world.sid, got["id"])

    assert done["status"] == "done"
    assert {a["id"]: a["user_id"] for a in done["authors"]}["a2"] is None
    assert message(client, "1002").metadata["imported_author"]["id"] == "a2"


# Re-mapping

def test_authors_can_be_remapped_after_the_import(client, world, roots):
    done = done_import(world, {"a1": world.alice_user["id"]})
    before = counts(client, world.sid)
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert message(client, "1002").author_id == system.id
    attachment = run(client, Attachment.get(message_id=message(client, "1002").id))
    assert attachment.uploader_id == system.id
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == system.id

    res = set_authors(client, world.sid, done["id"], {
        "a1": world.alice_user["id"], "a2": world.bob_user["id"], "a4": world.owner_user["id"]})
    assert res.status_code == 200
    assert res.json()["status"] == "importing"
    assert res.json()["progress"]["phase"] == "authors"
    after = settle(client, world.sid, done["id"])

    assert after["status"] == "done" and after["error"] is None and after["progress"] is None
    assert after["result"] == done["result"] and after["plan"] == done["plan"]
    assert counts(client, world.sid) == before
    handed = message(client, "1002")
    assert handed.author_id == world.bob_user["id"] and "imported_author" not in handed.metadata
    assert run(client, Attachment.get(id=attachment.id)).uploader_id == world.bob_user["id"]
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == world.owner_user["id"] and "imported_author" not in post.metadata
    assert message(client, "1003").author_id == system.id
    assert not (folder(roots, done["id"]) / "bundle" / "files").exists()

    back = set_authors(client, world.sid, done["id"], {"a1": world.alice_user["id"]})
    assert back.status_code == 200
    assert settle(client, world.sid, done["id"])["status"] == "done"
    returned = message(client, "1002")
    assert returned.author_id == system.id
    assert returned.metadata["imported_author"] == {"id": "a2", "name": "bob"}
    assert run(client, ForumPost.get(title="Look at this")).metadata["imported_author"] == {
        "id": "a4", "name": "dave"}
    assert counts(client, world.sid) == before


def test_unmapping_an_author_after_a_fully_mapped_import_hands_over_to_the_import_account(
        client, world):
    everyone = {"a1": world.alice_user["id"], "a2": world.bob_user["id"],
                "a3": world.alice_user["id"], "a4": world.bob_user["id"]}
    done = done_import(world, everyone)
    assert run(client, User.filter(username=IMPORTED_USERNAME).exists()) is False

    res = set_authors(client, world.sid, done["id"], {
        k: v for k, v in everyone.items() if k not in ("a2", "a4")})
    assert res.status_code == 200
    after = settle(client, world.sid, done["id"])

    assert after["status"] == "done" and after["error"] is None
    system = run(client, User.get(username=IMPORTED_USERNAME))
    handed = message(client, "1002")
    assert handed.author_id == system.id
    assert handed.metadata["imported_author"] == {"id": "a2", "name": "bob"}
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == system.id and post.metadata["imported_author"]["id"] == "a4"
    assert message(client, "1001").author_id == world.alice_user["id"]


def test_an_unchanged_mapping_runs_nothing(client, world, monkeypatch):
    mapping = {"a1": world.alice_user["id"]}
    done = done_import(world, mapping)
    started = []
    monkeypatch.setattr(ImportRunner, "start_handover", lambda self, i: started.append(i))

    res = set_authors(client, world.sid, done["id"], {**mapping, "a2": None})

    assert res.status_code == 200 and res.json()["status"] == "done"
    assert res.json()["progress"] is None
    assert started == []


def test_a_failed_hand_over_leaves_the_import_done_and_can_be_retried(client, world, monkeypatch):
    done = done_import(world)
    mapping = {"a2": world.bob_user["id"]}
    real = runner_module.import_bundle

    async def aborts(*args, **kwargs):
        raise ImportAborted("Nothing could be handed over")

    monkeypatch.setattr(runner_module, "import_bundle", aborts)
    assert set_authors(client, world.sid, done["id"], mapping).status_code == 200
    failed = settle(client, world.sid, done["id"])

    assert failed["status"] == "done" and failed["error"] == "Nothing could be handed over"
    assert failed["failed_step"] is None and failed["result"] == done["result"]
    assert {a["id"]: a["user_id"] for a in failed["authors"]}["a2"] == world.bob_user["id"]
    assert message(client, "1002").author_id != world.bob_user["id"]

    monkeypatch.setattr(runner_module, "import_bundle", real)
    assert set_authors(client, world.sid, done["id"], mapping).json()["status"] == "importing"
    fixed = settle(client, world.sid, done["id"])
    assert fixed["status"] == "done" and fixed["error"] is None
    assert message(client, "1002").author_id == world.bob_user["id"]


def test_remapping_is_refused_while_it_runs(client, world):
    done = done_import(world)
    force(client, done["id"], status="importing")

    assert set_authors(client, world.sid, done["id"], {}).status_code == 409


# A second upload of the same export

def test_a_second_upload_takes_the_mapping_over_and_adds_nothing(client, world, roots):
    first = done_import(world, {"a1": world.alice_user["id"], "a2": world.bob_user["id"]})
    run(client, UserToServer.filter(
        server_id=world.sid, user_id=world.bob_user["id"]).delete())
    before = counts(client, world.sid)

    second = ready_import(client, world.sid)

    assert second["id"] != first["id"]
    assert {a["id"]: a["user_id"] for a in second["authors"]} == {
        "a1": world.alice_user["id"], "a2": None, "a3": None, "a4": None}
    plan = second["plan"]
    assert plan["totals"]["messages"] == 0 and plan["totals"]["posts"] == 0
    assert plan["totals"]["existing_messages"] == 13
    actions = {c["name"]: c["action"] for c in plan["channels"]}
    assert actions == {
        "general": "existing", "staff": "skipped", "ideas": "existing", "Lounge": "existing",
        "off-topic": "existing", "archive": "skipped"}
    assert not any(c["name_taken"] for c in plan["channels"])
    assert sum(c["handed_over"] for c in plan["channels"]) > 0
    assert import_row(client, first["id"]) is not None

    assert start(client, world.sid, second["id"]).status_code == 202
    done = settle(client, world.sid, second["id"])

    assert done["status"] == "done"
    assert counts(client, world.sid) == before
    assert import_row(client, first["id"]) is None
    assert not folder(roots, first["id"]).exists()
    assert run(client, ServerImport.filter(server_id=world.sid).count()) == 1
    assert message(client, "1002").metadata["imported_author"]["id"] == "a2"
    assert message(client, "1001").author_id == world.alice_user["id"]


def test_the_mapping_is_not_taken_from_an_import_that_is_not_done(client, world):
    got = ready_import(client, world.sid)
    set_authors(client, world.sid, got["id"], {"a1": world.alice_user["id"]})

    again = ready_import(client, world.sid)

    assert all(a["user_id"] is None for a in again["authors"])


# Realtime

def frames_until(ws, stop):
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if stop(frame):
            return frames


def updated(frames) -> list[dict]:
    return [f for f in frames if f["type"] == "server_import_updated"]


def test_progress_goes_to_the_owner_and_the_end_to_every_member(client, world, monkeypatch):
    monkeypatch.setattr(runner_module, "PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(importer, "BATCH_SIZE", 2)

    with client.websocket_connect("/ws", headers=HEADERS) as owner_ws, \
            world.alice.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(owner_ws)
        ws_ready(alice_ws)

        got = ready_import(client, world.sid)
        frames = frames_until(
            owner_ws, lambda f: f["type"] == "server_import_updated"
            and f["import"]["status"] == "ready")
        shown = updated(frames)
        assert {f["server_id"] for f in shown} == {world.sid}
        assert {f["import"]["status"] for f in shown} >= {"unpacking", "ready"}
        assert all(f["import"]["id"] == got["id"] for f in shown)
        assert all(
            f["import"]["plan"] is None and f["import"]["result"] is None
            and f["import"]["authors"] == [] for f in shown)
        checking = [f["import"]["progress"] for f in shown
                    if (f["import"]["progress"] or {}).get("phase") == "checking"]
        assert checking and checking[-1]["done"] > 0 and checking[-1]["total"] == 0
        assert [p["done"] for p in checking] == sorted(p["done"] for p in checking)

        alice_ws.send_json({"type": "ping", "t": 1})
        earlier = frames_until(alice_ws, lambda f: f["type"] == "pong")
        assert not [f for f in earlier if f["type"].startswith("server_import")]

        assert start(client, world.sid, got["id"]).status_code == 202
        frames = frames_until(owner_ws, lambda f: f["type"] == "server_import_finished")
        assert frames[-1] == {"type": "server_import_finished", "server_id": world.sid}
        running = [f["import"]["progress"] for f in updated(frames)
                   if (f["import"]["progress"] or {}).get("phase") == "importing"]
        assert running and running[-1]["total"] == 13 and running[-1]["label"]
        assert [f["import"]["status"] for f in updated(frames)][-1] == "done"

        seen = frames_until(alice_ws, lambda f: f["type"] == "server_import_finished")
        assert seen[-1] == {"type": "server_import_finished", "server_id": world.sid}
        assert not updated(seen)


def test_a_remap_tells_the_members_when_it_is_done(client, world):
    done = done_import(world)

    with world.bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(bob_ws)
        set_authors(client, world.sid, done["id"], {"a2": world.bob_user["id"]})
        settle(client, world.sid, done["id"])
        frame = frames_until(bob_ws, lambda f: f["type"] == "server_import_finished")[-1]
        assert frame == {"type": "server_import_finished", "server_id": world.sid}


# Queue and shutdown

def hold_the_runner(client) -> None:
    client.portal.call(runner()._lock.acquire)


def release_the_runner(client) -> None:
    client.portal.call(runner()._lock.release)


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_a_job_waits_for_the_one_before_it(client, world):
    hold_the_runner(client)
    try:
        uploaded = upload(client, world.sid, zip_of())
        waiting = wait_for(lambda: (current(client, world.sid)["progress"] or {}).get("phase") == "queued")
        assert waiting
        assert current(client, world.sid)["status"] == "unpacking"
        assert client.portal.call(runner().start_unpack, UUID(uploaded["id"])) is False
    finally:
        release_the_runner(client)

    assert settle(client, world.sid, uploaded["id"])["status"] == "ready"


def test_a_shutdown_leaves_the_import_as_it_was_and_a_start_picks_it_up(client, world):
    hold_the_runner(client)
    uploaded = upload(client, world.sid, zip_of())
    wait_for(lambda: runner().is_running(UUID(uploaded["id"])))

    client.portal.call(runner().shutdown)

    assert not runner().is_running(UUID(uploaded["id"]))
    assert current(client, world.sid)["status"] == "unpacking"
    release_the_runner(client)
    client.portal.call(runner().resume)
    assert settle(client, world.sid, uploaded["id"])["status"] == "ready"


# Startup

def test_an_import_interrupted_while_importing_finishes_after_a_restart(client, world):
    got = ready_import(client, world.sid)
    force(client, got["id"], status="importing", progress={
        "phase": "importing", "done": 4, "total": 13, "label": "general"})

    client.portal.call(runner().resume)
    done = settle(client, world.sid, got["id"])

    assert done["status"] == "done" and counts(client, world.sid)[2] == 13
    assert done["plan"] == got["plan"]


def test_an_upload_interrupted_while_unpacking_starts_over(client, world, roots):
    data = zip_of()
    imp = create(client, world.sid, len(data)).json()
    (folder(roots, imp["id"])).mkdir(parents=True)
    (folder(roots, imp["id"]) / "upload.zip").write_bytes(data)
    (folder(roots, imp["id"]) / "bundle").mkdir()
    (folder(roots, imp["id"]) / "bundle" / "leftover.txt").write_text("half done")
    force(client, imp["id"], status="unpacking", received=len(data))

    client.portal.call(runner().resume)
    got = settle(client, world.sid, imp["id"])

    assert got["status"] == "ready" and got["plan"]["totals"]["messages"] == 13
    assert not (folder(roots, imp["id"]) / "bundle" / "leftover.txt").exists()
    assert not (folder(roots, imp["id"]) / "upload.zip").exists()


def test_a_check_interrupted_after_unpacking_is_only_redone(client, world):
    got = ready_import(client, world.sid)
    force(client, got["id"], status="unpacking", plan=None, source=None, source_platform=None,
          source_name=None, progress={"phase": "checking", "done": 3, "total": 0, "label": None})

    client.portal.call(runner().resume)
    again = settle(client, world.sid, got["id"])

    assert again["status"] == "ready" and again["plan"]["totals"]["messages"] == 13


def test_an_unpacking_import_with_nothing_left_on_disk_fails(client, world, roots):
    imp = create(client, world.sid, 100).json()
    force(client, imp["id"], status="unpacking", received=100)

    client.portal.call(runner().resume)
    got = settle(client, world.sid, imp["id"])

    assert (got["status"], got["failed_step"]) == ("failed", "unpacking")
    assert "upload it again" in got["error"]


@pytest.mark.parametrize("phase", ["authors", "queued"])
def test_a_hand_over_interrupted_by_a_restart_is_finished(client, world, phase):
    done = done_import(world)
    force(client, done["id"], status="importing", authors={"a2": world.bob_user["id"]},
          progress={"phase": phase, "done": 0, "total": 13, "label": None})

    client.portal.call(runner().resume)
    after = settle(client, world.sid, done["id"])

    assert after["status"] == "done" and after["result"] == done["result"]
    assert message(client, "1002").author_id == world.bob_user["id"]
    assert counts(client, world.sid)[2] == 13


def test_a_restart_drops_stale_uploads_and_folders_nobody_owns(client, world, new_client, roots):
    other = create_server(client, "Second")
    stale = create(client, world.sid, 100).json()
    fresh = create(client, other["id"], 100).json()
    kept = ready_import(client, create_server(client, "Third")["id"])
    for import_id in (stale["id"], fresh["id"]):
        put_piece(client, world.sid if import_id == stale["id"] else other["id"],
                  import_id, 0, b"x" * 10)
    force(client, stale["id"], updated_at=datetime.now(timezone.utc) - timedelta(hours=25))
    orphan = roots.imports / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "upload.zip").write_bytes(b"x")
    stranger = roots.imports / "notes"
    stranger.mkdir()

    client.portal.call(runner().prune)

    assert import_row(client, stale["id"]) is None and not folder(roots, stale["id"]).exists()
    assert import_row(client, fresh["id"]) is not None and folder(roots, fresh["id"]).exists()
    assert import_row(client, kept["id"]) is not None and folder(roots, kept["id"]).exists()
    assert not orphan.exists()
    assert stranger.exists()


# Deleting

def test_deleting_an_import_removes_its_files_but_not_what_it_imported(client, world, roots):
    done = done_import(world)
    before = counts(client, world.sid)
    assert folder(roots, done["id"]).exists()

    assert client.delete(url(world.sid, done["id"])).status_code == 204

    assert current(client, world.sid) is None
    assert not folder(roots, done["id"]).exists()
    assert counts(client, world.sid) == before
    assert client.delete(url(world.sid, done["id"])).status_code == 404


def test_an_upload_can_be_abandoned(client, world, roots):
    imp = create(client, world.sid, 100).json()
    put_piece(client, world.sid, imp["id"], 0, b"x" * 10)

    assert client.delete(url(world.sid, imp["id"])).status_code == 204
    assert not folder(roots, imp["id"]).exists()
    assert put_piece(client, world.sid, imp["id"], 10, b"x").status_code == 404


@pytest.mark.parametrize("status", ["unpacking", "importing"])
def test_a_running_import_cannot_be_deleted(client, world, status):
    imp = create(client, world.sid, 100).json()
    force(client, imp["id"], status=status)

    res = client.delete(url(world.sid, imp["id"]))

    assert (res.status_code, res.json()["detail"]) == (409, "The import is running")
    assert import_row(client, imp["id"]) is not None


def test_deleting_the_server_removes_its_import_files(client, world, roots):
    got = ready_import(client, world.sid)
    assert folder(roots, got["id"]).exists()

    assert client.delete(f"/servers/{world.sid}").status_code == 204

    assert not folder(roots, got["id"]).exists()
    assert import_row(client, got["id"]) is None


def test_deleting_the_server_stops_its_running_import(client, world, roots):
    hold_the_runner(client)
    try:
        uploaded = upload(client, world.sid, zip_of())
        wait_for(lambda: runner().is_running(UUID(uploaded["id"])))

        assert client.delete(f"/servers/{world.sid}").status_code == 204

        assert not runner().is_running(UUID(uploaded["id"]))
        assert not folder(roots, uploaded["id"]).exists()
    finally:
        release_the_runner(client)


# Private channels

SECRET = b"the staff rota"


def private_bundle(tmp_path, *, attachment=False, author=None, unreadable=False) -> Path:
    """The sample bundle, with the staff channel (103) carrying a file, written
    by an author who posts nowhere else, or unreadable."""
    target = tmp_path / "private-bundle"
    shutil.copytree(FIXTURE, target)
    path = target / "channels" / "103" / "messages" / "1.json"
    messages = json.loads(path.read_text())
    data = json.loads((target / "server.json").read_text())
    if attachment:
        (target / "files" / "9003").mkdir()
        (target / "files" / "9003" / "rota.txt").write_bytes(SECRET)
        messages[0]["attachments"] = [{
            "id": "9003", "filename": "rota.txt", "size": len(SECRET),
            "content_type": "text/plain", "path": "files/9003/rota.txt"}]
    if author:
        messages[0]["author_id"] = author
        data["authors"].append({"id": author, "name": "erin", "avatar": None, "messages": 1})
    if unreadable:
        data["unreadable"].append({"id": "103", "name": "staff"})
    path.write_text(json.dumps(messages))
    (target / "server.json").write_text(json.dumps(data))
    return target


def staff_row(plan_or_result) -> dict:
    return next(c for c in plan_or_result["channels"] if c["source_id"] == "103")


def staff_channel(client, sid) -> Channel:
    return run(client, Channel.get(server_id=sid, name="staff"))


def overwrite_count(client, sid) -> int:
    return run(client, PermissionOverwrite.filter(server_id=sid).count())


def test_the_plan_offers_the_private_channel_as_selectable(client, world):
    got = ready_import(client, world.sid)

    assert staff_row(got["plan"]) == {
        "source_id": "103", "name": "staff", "type": "text", "action": "skipped",
        "target_name": None, "name_taken": False, "reason": "private",
        "category": "Text channels", "messages": 1, "existing_messages": 0, "posts": 0,
        "existing_posts": 0, "attachments": 0, "attachment_bytes": 0, "handed_over": 0,
        "private": True, "private_action": "create", "visibility": None}
    others = [c for c in got["plan"]["channels"] if c["source_id"] != "103"]
    assert all(c["private"] is False and c["private_action"] is None for c in others)
    assert all(c["visibility"] is None for c in got["plan"]["channels"])
    assert got["plan"]["totals"]["messages"] == 13
    assert got["plan"]["left_out"]["private_channels"] == 1
    assert not {"private_seen", "private_selection", "authors", "seen"} & set(got["plan"])
    assert got["private_channels"] == {}


def test_a_private_channel_that_cannot_be_imported_is_not_selectable(client, world, tmp_path):
    bundle = private_bundle(tmp_path, unreadable=True)
    got = ready_import(client, world.sid, zip_of(bundle))

    row = staff_row(got["plan"])
    assert (row["private"], row["private_action"], row["reason"], row["messages"]) == (
        True, None, "unreadable", 0)
    refused = start(client, world.sid, got["id"], {"private_channels": {"103": "only_me"}})
    assert refused.status_code == 422


def test_an_author_who_only_posts_in_a_private_channel_can_be_mapped(client, world, tmp_path):
    got = ready_import(client, world.sid, zip_of(private_bundle(tmp_path, author="a5")))

    assert authors_by_id(got)["a5"]["messages"] == 1
    keys = [(-a["messages"], a["name"]) for a in got["authors"]]
    assert keys == sorted(keys)
    assert set_authors(client, world.sid, got["id"], {"a5": world.bob_user["id"]}).status_code == 200


@pytest.mark.parametrize("body", [None, {}, {"private_channels": {}}, {"private_channels": None}])
def test_start_without_a_selection_imports_no_private_channel(client, world, body):
    got = ready_import(client, world.sid)

    started = start(client, world.sid, got["id"], body)

    assert started.status_code == 202, started.text
    assert started.json()["private_channels"] == {}
    done = settle(client, world.sid, got["id"])
    assert not run(client, Channel.filter(server_id=world.sid, name="staff").exists())
    assert done["private_channels"] == {} and done["result"]["totals"]["messages"] == 13
    assert staff_row(done["result"])["reason"] == "private"
    assert done["result"]["left_out"]["private_channels"] == 1


def test_start_with_a_selection_imports_the_private_channel_only_for_the_owner(client, world):
    got = ready_import(client, world.sid)

    started = start(client, world.sid, got["id"], {"private_channels": {"103": "only_me"}})

    assert started.status_code == 202, started.text
    assert started.json()["private_channels"] == {"103": "only_me"}
    assert started.json()["progress"] == {
        "phase": "importing", "done": 0, "total": 14, "label": None}
    assert not {"private_seen", "private_selection"} & set(started.json()["plan"])
    done = settle(client, world.sid, got["id"])

    assert done["private_channels"] == {"103": "only_me"}
    staff = staff_channel(client, world.sid)
    everyone = run(client, Role.get(server_id=world.sid, is_default=True))
    rows = run(client, PermissionOverwrite.filter(server_id=world.sid))
    assert [(r.channel_id, r.role_id, r.user_id, r.allow, r.deny) for r in rows] == [
        (staff.id, everyone.id, None, 0, int(Permission.VIEW_CHANNEL))]
    row = staff_row(done["result"])
    assert (row["action"], row["private"], row["visibility"], row["private_action"]) == (
        "create", True, "only_me", None)
    assert done["result"]["totals"]["messages"] == 14
    assert done["result"]["left_out"]["private_channels"] == 0
    assert not {"private_seen", "private_selection"} & set(done["plan"])


def test_a_private_channel_can_be_made_visible_to_everyone(client, world):
    got = ready_import(client, world.sid)
    body = {"private_channels": {"103": "everyone"}}
    assert start(client, world.sid, got["id"], body).status_code == 202
    done = settle(client, world.sid, got["id"])

    assert staff_row(done["result"])["visibility"] == "everyone"
    assert overwrite_count(client, world.sid) == 0
    staff = staff_channel(client, world.sid)
    assert world.alice.get(f"/channels/{staff.id}/messages").status_code == 200


@pytest.mark.parametrize("selection, detail", [
    ({"999": "only_me"}, "999 is not a private channel of this import"),
    ({"101": "only_me"}, "101 is not a private channel of this import"),
    ({"103": "secret"}, None),
    ({"103": None}, None),
])
def test_a_bad_selection_is_refused_and_the_import_stays_ready(client, world, selection, detail):
    got = ready_import(client, world.sid)

    refused = start(client, world.sid, got["id"], {"private_channels": selection})

    assert refused.status_code == 422
    if detail:
        assert refused.json()["detail"] == detail
    assert current(client, world.sid)["status"] == "ready"
    assert current(client, world.sid)["private_channels"] == {}


def test_start_checks_the_status_before_the_selection(client, world):
    uploading = create(client, world.sid, 100).json()

    refused = start(client, world.sid, uploading["id"], {"private_channels": {"999": "only_me"}})

    assert refused.status_code == 409


def failing_once(monkeypatch):
    real = runner_module.import_bundle
    calls = []

    async def fails_once(bundle, options, progress=None):
        calls.append(options)
        report = await real(bundle, options, progress)
        if len(calls) == 1:
            raise RuntimeError("disk exploded")
        return report

    monkeypatch.setattr(runner_module, "import_bundle", fails_once)
    return calls


def test_a_retry_without_a_body_reuses_the_selection(client, world, monkeypatch):
    got = ready_import(client, world.sid)
    calls = failing_once(monkeypatch)
    body = {"private_channels": {"103": "everyone"}}
    assert start(client, world.sid, got["id"], body).status_code == 202
    failed = settle(client, world.sid, got["id"])
    assert failed["status"] == "failed" and failed["private_channels"] == {"103": "everyone"}

    retried = start(client, world.sid, got["id"])

    assert retried.status_code == 202
    assert retried.json()["private_channels"] == {"103": "everyone"}
    done = settle(client, world.sid, got["id"])
    assert calls[1].private == {"103": "everyone"}
    assert staff_row(done["result"])["action"] == "existing"
    assert run(client, Channel.filter(server_id=world.sid, name="staff").count()) == 1


def test_a_retry_with_a_body_replaces_the_selection(client, world, monkeypatch):
    got = ready_import(client, world.sid)
    calls = failing_once(monkeypatch)
    start(client, world.sid, got["id"], {"private_channels": {"103": "everyone"}})
    assert settle(client, world.sid, got["id"])["status"] == "failed"

    retried = start(client, world.sid, got["id"], {"private_channels": {"103": "only_me"}})

    assert retried.json()["private_channels"] == {"103": "only_me"}
    settle(client, world.sid, got["id"])
    assert calls[1].private == {"103": "only_me"}


def test_a_retry_with_an_empty_selection_drops_it(client, world, monkeypatch):
    got = ready_import(client, world.sid)
    calls = failing_once(monkeypatch)
    start(client, world.sid, got["id"], {"private_channels": {"103": "everyone"}})
    settle(client, world.sid, got["id"])
    run(client, Channel.filter(server_id=world.sid, name="staff").delete())

    retried = start(client, world.sid, got["id"], {"private_channels": {}})

    assert retried.json()["private_channels"] == {}
    settle(client, world.sid, got["id"])
    assert calls[1].private == {}
    assert not run(client, Channel.filter(server_id=world.sid, name="staff").exists())


def test_a_changed_visibility_does_not_re_permission_a_channel_an_earlier_run_created(
        client, world, monkeypatch):
    got = ready_import(client, world.sid)
    calls = failing_once(monkeypatch)
    start(client, world.sid, got["id"], {"private_channels": {"103": "only_me"}})
    assert settle(client, world.sid, got["id"])["status"] == "failed"
    staff = staff_channel(client, world.sid)
    rows = lambda: sorted(run(client, PermissionOverwrite.filter(  # noqa: E731
        channel_id=staff.id).values_list("id", "deny")))
    before = rows()
    assert len(before) == 1

    start(client, world.sid, got["id"], {"private_channels": {"103": "everyone"}})
    done = settle(client, world.sid, got["id"])

    assert calls[1].private == {"103": "everyone"}
    row = staff_row(done["result"])
    assert (row["action"], row["visibility"]) == ("existing", None)
    assert rows() == before


def test_the_disk_check_counts_the_selected_private_attachments(client, world, tmp_path, monkeypatch):
    got = ready_import(client, world.sid, zip_of(private_bundle(tmp_path, attachment=True)))
    assert staff_row(got["plan"])["attachment_bytes"] == len(SECRET)
    free = got["plan"]["totals"]["attachment_bytes"] + ATTACHMENT_SPACE_MARGIN + len(SECRET) - 1

    with monkeypatch.context() as patched:
        patched.setattr(
            shutil, "disk_usage", lambda path: SimpleNamespace(total=10, used=10, free=free))
        refused = start(client, world.sid, got["id"], {"private_channels": {"103": "only_me"}})
        assert (refused.status_code, refused.json()["detail"]) == (
            409, "Not enough disk space for the attachments")
        assert current(client, world.sid)["status"] == "ready"
        assert current(client, world.sid)["private_channels"] == {}
        assert start(client, world.sid, got["id"]).status_code == 202
    assert settle(client, world.sid, got["id"])["status"] == "done"


def test_remapping_hands_over_messages_in_the_private_channel(client, world):
    got = ready_import(client, world.sid)
    body = {"private_channels": {"103": "only_me"}}
    assert start(client, world.sid, got["id"], body).status_code == 202
    done = settle(client, world.sid, got["id"])
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert message(client, "3001").author_id == system.id

    res = set_authors(client, world.sid, done["id"], {"a1": world.alice_user["id"]})

    assert res.status_code == 200
    after = settle(client, world.sid, done["id"])
    assert after["status"] == "done" and after["private_channels"] == {"103": "only_me"}
    assert message(client, "3001").author_id == world.alice_user["id"]
    assert overwrite_count(client, world.sid) == 1


def test_a_second_upload_can_add_only_the_private_channel(client, world):
    first = done_import(world)
    before = counts(client, world.sid)
    assert staff_row(first["result"])["reason"] == "private"

    second = ready_import(client, world.sid)
    assert staff_row(second["plan"])["private_action"] == "create"
    assert second["plan"]["totals"]["messages"] == 0
    body = {"private_channels": {"103": "only_me"}}
    assert start(client, world.sid, second["id"], body).status_code == 202
    done = settle(client, world.sid, second["id"])

    after = counts(client, world.sid)
    assert after[0] == before[0] + 1 and after[2] == before[2] + 1
    assert after[1] == before[1] and after[3:] == before[3:]
    assert staff_row(done["result"])["action"] == "create"
    assert done["result"]["totals"]["messages"] == 1


def test_a_second_upload_continues_in_the_private_channel_an_earlier_one_created(client, world):
    first = ready_import(client, world.sid)
    start(client, world.sid, first["id"], {"private_channels": {"103": "everyone"}})
    settle(client, world.sid, first["id"])
    before = counts(client, world.sid)

    second = ready_import(client, world.sid)

    row = staff_row(second["plan"])
    assert (row["private_action"], row["target_name"], row["existing_messages"]) == (
        "existing", "staff", 1)
    assert start(client, world.sid, second["id"], {"private_channels": {"103": "only_me"}}
                 ).status_code == 202
    done = settle(client, world.sid, second["id"])
    assert counts(client, world.sid) == before
    result = staff_row(done["result"])
    assert (result["action"], result["visibility"]) == ("existing", None)
    assert overwrite_count(client, world.sid) == 0


# An only-me channel does not leak

def frame_of_type(ws, kind):
    while True:
        frame = ws.receive_json()
        if frame["type"] == kind:
            return frame


def quiet_after(ws, ping_id):
    """Everything the socket received up to the answer to a ping sent now."""
    ws.send_json({"type": "ping", "t": ping_id})
    received = []
    while True:
        frame = ws.receive_json()
        if frame == {"type": "pong", "t": ping_id}:
            return received
        received.append(frame)


def read_surfaces(member, sid) -> dict:
    server = member.get(f"/servers/{sid}").json()
    with member.websocket_connect("/ws", headers=HEADERS) as ws:
        init = frame_of_type(ws, "permissions_init")
    return {
        "channels": member.get(f"/channels/{sid}").json(),
        "layout": member.get(f"/servers/{sid}/channels").json(),
        "server": server,
        "init": init,
    }


def test_an_only_me_channel_is_invisible_to_members(client, world, tmp_path):
    sid = world.sid
    got = ready_import(client, sid, zip_of(private_bundle(tmp_path, attachment=True)))
    for member in (world.alice, world.bob):
        read_surfaces(member, sid)

    assert start(client, sid, got["id"], {"private_channels": {"103": "only_me"}}
                 ).status_code == 202
    assert settle(client, sid, got["id"])["status"] == "done"
    staff = staff_channel(client, sid)
    attachment = run(client, Attachment.get(channel_id=staff.id))

    for member in (world.alice, world.bob):
        surfaces = read_surfaces(member, sid)
        assert staff.id not in [c["id"] for c in surfaces["channels"]]
        assert staff.id not in [c["id"] for c in surfaces["layout"]["channels"]]
        assert staff.id not in surfaces["server"]["server_settings"]["channel_order"]
        assert str(staff.id) not in surfaces["server"]["channel_permissions"]
        assert str(staff.id) not in surfaces["init"]["channels"][str(sid)]
        assert "staff" not in json.dumps(surfaces)

        assert member.get(f"/channels/{staff.id}/messages").status_code == 404
        assert member.get(f"/attachments/{attachment.id}").status_code == 404
        assert member.post(f"/attachments/{attachment.id}/link").status_code == 404

    assert staff.id in [c["id"] for c in world.owner.get(f"/channels/{sid}").json()]
    history = world.owner.get(f"/channels/{staff.id}/messages")
    assert history.status_code == 200 and len(history.json()["messages"]) == 1
    fetched = world.owner.get(f"/attachments/{attachment.id}")
    assert fetched.status_code == 200 and fetched.content == SECRET
    assert world.owner.post(f"/attachments/{attachment.id}/link").status_code == 200


def test_an_only_me_channel_sends_members_no_frames(client, world):
    sid = world.sid
    got = ready_import(client, sid)
    start(client, sid, got["id"], {"private_channels": {"103": "only_me"}})
    settle(client, sid, got["id"])
    staff = staff_channel(client, sid)

    with world.alice.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            world.owner.websocket_connect("/ws", headers=HEADERS) as owner_ws:
        ws_ready(alice_ws)
        ws_ready(owner_ws)
        owner_ws.send_json({"type": "typing", "server_id": sid, "channel_id": staff.id})
        post = chat_frame(sid, staff.id, "owner only")
        owner_ws.send_json(post)
        while True:
            reply = owner_ws.receive_json()
            if reply.get("type") == "message_ack" and reply.get("id") == post["id"]:
                break
        frames = quiet_after(alice_ws, 1)

    assert [f for f in frames if f["type"] in ("message", "typing")] == []
    assert str(staff.id) not in json.dumps(frames)
