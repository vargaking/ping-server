"""Importing a server bundle, against the sample bundle in tests/fixtures."""
import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.Attachment import Attachment
from app.models.Channel import Channel
from app.models.ChannelGroup import ChannelGroup
from app.models.ForumPost import ForumPost
from app.models.ForumTag import ForumTag
from app.models.Message import Message
from app.models.PermissionOverwrite import PermissionOverwrite
from app.models.ReadState import ReadState
from app.models.Role import Role
from app.models.Server import Server
from app.models.User import User
from app.models.UserToServer import UserToServer
from app.permissions import Permission
from app.scripts import import_bundle as import_cli
from app.services.bundle import importer
from app.services.bundle.format import BundleError, iter_channel_messages, load_server
from app.services.bundle.plan import plan_json
from app.services.bundle.importer import (
    ImportAborted,
    ImportOptions,
    format_report,
    import_bundle,
    message_uuid,
)
from app.services.chat_service import ChatService
from app.services.system_user import IMPORTED_PASSWORD_HASH, IMPORTED_USERNAME
from app.ws_schemas import DirectMessageFrame, MessageFrame
from tests.conftest import ORIGIN, PASSWORD, create_channel, create_server, register, ws_ready
from tests.test_permissions import join
from tests.test_server_requests import make_admin

FIXTURE = Path(__file__).parent / "fixtures" / "bundle"
SOURCE = "discord:9000"
HEADERS = {"origin": ORIGIN}


def run(client, awaitable):
    async def wait():
        return await awaitable
    return client.portal.call(wait)


def messages_of(client, **filters):
    return run(client, Message.filter(**filters).order_by("timestamp", "id"))


def message(client, source_id, sid=None):
    if sid is None:
        sid = run(client, Server.all().order_by("id").first()).id
    return run(client, Message.get(uuid=message_uuid(sid, SOURCE, source_id)))


def channel_named(client, sid, name):
    return run(client, Channel.get(server_id=sid, name=name))


def mark_imported(client, channel_id, source_id, **marker):
    """Make a channel look like an earlier run merged into it, with the single-id marker."""
    channel = run(client, Channel.get(id=channel_id))
    channel.channel_settings = {
        **channel.channel_settings, "import": {"source": SOURCE, "id": source_id, **marker}}
    run(client, channel.save())


def channel_marker(client, channel_id):
    return run(client, Channel.get(id=channel_id)).channel_settings["import"]


def user_id(client, username):
    return run(client, User.get(username=username)).id


@pytest.fixture(autouse=True)
def attachments_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    return tmp_path / "attachments"


@pytest.fixture
def bundle(tmp_path):
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def world(client, new_client):
    """A server with an owner and two members (alice and bob), plus carol,
    who is not in the server."""
    register(client, "owner-bundle")
    server = create_server(client)
    alice_client, alice = join(client, new_client, server["id"])
    bob_client, bob = join(client, new_client, server["id"])
    carol_client = new_client()
    carol = register(carol_client, "carol-outside")
    return SimpleNamespace(
        owner=client, sid=server["id"], alice=alice_client, alice_user=alice,
        bob=bob_client, bob_user=bob, carol_user=carol)


def do_import(client, bundle, sid, **options):
    return run(client, import_bundle(bundle, ImportOptions(server_id=sid, **options)))


def authors(world, **extra):
    return {"a1": world.alice_user["username"], **extra}


def edit_server_json(bundle, change):
    path = bundle / "server.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def plans(report):
    return {c.name: c.plan for c in report.channels}


# Full import

def test_full_import(client, world, bundle):
    report = do_import(client, bundle, world.sid, authors=authors(world))

    assert plans(report) == {
        "general": "create", "staff": "skipped: private", "ideas": "create",
        "Lounge": "create", "off-topic": "create", "archive": "skipped: unreadable"}
    by_name = {c.name: c for c in report.channels}
    assert by_name["general"].messages == 7
    assert by_name["general"].attachments == 2
    assert by_name["ideas"].posts == 2 and by_name["ideas"].messages == 5
    assert by_name["off-topic"].messages == 1
    assert by_name["Lounge"].messages == 0
    assert report.messages == 13 and report.posts == 2
    assert report.missing == 1 and report.over_cap == 0
    assert report.left_out.voice_text_chat == 1
    assert report.left_out.private_channels == 1
    assert report.left_out.text_channel_threads == 1
    assert report.left_out.messages_with_reactions == 1
    assert report.left_out.pinned_messages == 1
    assert report.left_out.emoji == 1 and report.left_out.avatars == 2
    assert report.left_out.unreadable_channels == 1

    general = channel_named(client, world.sid, "general")
    assert general.topic == "Welcome to the sample server"
    assert general.channel_settings["import"] == {
        "source": SOURCE, "id": "101", "ids": ["101"], "created": True}
    text_group = run(client, ChannelGroup.get(server_id=world.sid, name="Text channels"))
    voice_group = run(client, ChannelGroup.get(server_id=world.sid, name="Voice channels"))
    assert general.group_id == text_group.id
    assert channel_named(client, world.sid, "Lounge").group_id == voice_group.id
    assert channel_named(client, world.sid, "off-topic").group_id is None
    assert run(client, ChannelGroup.filter(server_id=world.sid).count()) == 2
    assert run(client, Channel.filter(server_id=world.sid, name="staff").exists()) is False


def test_messages_keep_their_times_and_fields(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))

    first = message(client, "1001")
    expected = datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)
    assert first.timestamp == expected and first.created_at == expected
    edited = message(client, "1004")
    assert edited.edited_at == datetime(2024, 3, 1, 10, 12, tzinfo=timezone.utc)
    assert json.loads(edited.content)["content"][0]["content"][1]["marks"] == [{"type": "bold"}]
    assert first.metadata["import"] == {"source": SOURCE, "id": "1001"}
    assert first.server_id == world.sid and first.post_id is None

    reply = message(client, "1002")
    assert reply.reply_to_uuid == first.uuid
    assert message(client, "1001").reply_to_uuid is None


def test_content_is_converted_and_normalised(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))

    content = json.loads(message(client, "1006").content)
    assert [b["type"] for b in content["content"]] == ["blockquote", "paragraph", "codeBlock"]
    mention = json.loads(message(client, "1002").content)["content"][0]["content"][1]
    assert mention == {"type": "mention", "attrs": {
        "id": str(world.alice_user["id"]), "label": world.alice_user["username"]}}


def test_attachments_are_stored_as_normal_attachments(client, world, bundle, attachments_dir):
    do_import(client, bundle, world.sid, authors=authors(world))

    image = run(client, Attachment.get(message_id=message(client, "1002").id))
    assert (image.kind, image.content_type, image.width, image.height) == ("image", "image/png", 4, 3)
    assert image.filename == "cat.png" and image.channel_id == message(client, "1002").channel_id
    assert (attachments_dir / image.storage_path).read_bytes() == (
        FIXTURE / "files/9001/cat.png").read_bytes()
    notes = run(client, Attachment.get(message_id=message(client, "1006").id))
    assert (notes.kind, notes.content_type) == ("file", "text/plain")
    assert run(client, Attachment.filter(message_id=message(client, "1003").id).count()) == 0

    res = world.alice.get(f"/attachments/{image.id}")
    assert res.status_code == 200


def test_embeds_come_from_the_bundle(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))

    assert message(client, "1007").metadata["embeds"] == [{
        "url": "https://example.com/docs/x_y_z", "title": "Docs", "description": "The docs",
        "site_name": "Example", "image_url": None}]
    assert message(client, "8201").metadata["embeds"][0]["image_url"] == "https://example.com/cool.png"


def test_forum_posts(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))

    ideas = channel_named(client, world.sid, "ideas")
    tags = {t.name: t.id for t in run(client, ForumTag.filter(channel_id=ideas.id))}
    assert set(tags) == {"idea", "bug"}
    posts = {p.title: p for p in run(client, ForumPost.filter(channel_id=ideas.id))}
    dark, cool = posts["Dark mode?"], posts["Look at this"]
    assert dark.pinned and not dark.locked and cool.locked
    assert dark.created_at == datetime(2024, 3, 5, 9, 0, tzinfo=timezone.utc)
    assert dark.last_activity_at == datetime(2024, 3, 6, 14, 0, tzinfo=timezone.utc)
    assert dark.reply_count == 2 and cool.reply_count == 1
    assert dark.opening_message_id == message(client, "8101").id
    assert dark.metadata["import"] == {"source": SOURCE, "id": "8001"}
    assert dark.author_id == world.alice_user["id"]
    assert {m.post_id for m in messages_of(client, post_id=dark.id)} == {dark.id}
    res = world.alice.get(f"/channels/{ideas.id}/posts")
    assert res.status_code == 200, res.text
    rows = {r["title"]: r for r in res.json()["posts"]}
    assert sorted(rows["Look at this"]["tag_ids"]) == sorted(tags.values())
    assert rows["Look at this"]["thumbnail"] == {"type": "embed", "url": "https://example.com/cool.png"}
    assert rows["Dark mode?"]["imported_author"] is None
    assert rows["Look at this"]["imported_author"] == {"id": "a4", "name": "dave"}


def test_dry_run_writes_nothing_and_matches_a_real_run(client, world, bundle, attachments_dir):
    def counts():
        return [run(client, model.all().count()) for model in (
            Channel, ChannelGroup, Message, Attachment, ForumPost, ForumTag, User, ReadState)]

    before = counts()
    dry = do_import(client, bundle, world.sid, authors=authors(world), dry_run=True)
    assert counts() == before
    assert not any(p.is_file() for p in attachments_dir.rglob("*"))
    assert run(client, User.filter(username=IMPORTED_USERNAME).exists()) is False

    real = do_import(client, bundle, world.sid, authors=authors(world))
    assert dry.dry_run and not real.dry_run
    assert (dry.messages, dry.posts, dry.attachments, dry.attachment_bytes) == (
        real.messages, real.posts, real.attachments, real.attachment_bytes)
    assert plans(dry) == plans(real)
    assert [(a.id, a.messages) for a in dry.unmapped_authors] == [
        (a.id, a.messages) for a in real.unmapped_authors]
    assert dry.free_bytes > 0
    assert "(dry run, nothing written)" in format_report(dry)


# Channel plan

def test_channel_plan_map_earlier_import_and_new(client, world, bundle):
    owner = world.owner
    general = create_channel(owner, world.sid, "general")
    other = create_channel(owner, world.sid, "somewhere-else")
    create_channel(owner, world.sid, "ideas", "text")  # same name, other type

    dry = do_import(
        client, bundle, world.sid, dry_run=True, channel_map={"106": other["id"]})
    assert plans(dry) == {
        "general": "create", "staff": "skipped: private",
        "ideas": "create", "Lounge": "create",
        "off-topic": "into existing #somewhere-else", "archive": "skipped: unreadable"}
    by_name = {c.name: c for c in dry.channels}
    assert all(c.matched_by is None for c in dry.channels if c.name != "off-topic")
    assert by_name["off-topic"].matched_by == "map"
    assert {n: c.name_taken for n, c in by_name.items()} == {
        "general": True, "staff": False, "ideas": True, "Lounge": False,
        "off-topic": False, "archive": False}

    do_import(client, bundle, world.sid, channel_map={"106": other["id"]})
    assert messages_of(client, channel_id=general["id"]) == []
    assert run(client, Channel.get(id=general["id"])).channel_settings == {}
    assert len(messages_of(client, channel_id=other["id"])) == 1
    assert channel_marker(client, other["id"]) == {"source": SOURCE, "id": "106", "ids": ["106"]}
    created = run(client, Channel.filter(server_id=world.sid, name="general").order_by("id"))
    assert len(created) == 2
    imported = created[1]
    assert len(messages_of(client, channel_id=imported.id)) == 7
    assert imported.topic == "Welcome to the sample server"


def test_a_name_never_merges_into_an_existing_channel(client, world, bundle):
    general = create_channel(world.owner, world.sid, "general")
    before = run(client, Channel.filter(server_id=world.sid).count())

    report = do_import(client, bundle, world.sid)

    row = {c.name: c for c in report.channels}["general"]
    assert (row.action, row.name_taken, row.channel_id != general["id"]) == ("create", True, True)
    assert run(client, Channel.filter(server_id=world.sid).count()) == before + 4
    assert messages_of(client, channel_id=general["id"]) == []
    assert run(client, Channel.get(id=general["id"])).channel_settings == {}


def test_name_taken_ignores_case_and_type_and_only_applies_to_creates(client, world, bundle):
    create_channel(world.owner, world.sid, "GENERAL", "voice")
    other = create_channel(world.owner, world.sid, "ideas", "forum")

    dry = do_import(
        client, bundle, world.sid, dry_run=True, channel_map={"105": other["id"]},
        only={"101", "105"})
    by_name = {c.name: c for c in dry.channels}
    assert (by_name["general"].action, by_name["general"].name_taken) == ("create", True)
    assert (by_name["ideas"].action, by_name["ideas"].name_taken) == ("existing", False)
    assert (by_name["staff"].action, by_name["staff"].name_taken) == ("skipped", False)
    assert "a channel with this name already exists" in format_report(dry)


def test_rerun_continues_in_the_channel_it_created(client, world, bundle):
    create_channel(world.owner, world.sid, "general")
    do_import(client, bundle, world.sid)
    count = run(client, Channel.filter(server_id=world.sid).count())

    again = do_import(client, bundle, world.sid)

    row = {c.name: c for c in again.channels}["general"]
    assert (row.action, row.name_taken, row.matched_by) == ("existing", False, "earlier import")
    assert again.messages == 0
    assert run(client, Channel.filter(server_id=world.sid).count()) == count
    assert run(client, Channel.filter(server_id=world.sid, name="general").count()) == 2


def test_map_wins_over_an_earlier_import(client, world, bundle):
    do_import(client, bundle, world.sid)
    target = create_channel(world.owner, world.sid, "target")
    dry = do_import(client, bundle, world.sid, dry_run=True, channel_map={"101": target["id"]})
    assert plans(dry)["general"] == "into existing #target"


def test_a_channel_an_old_run_merged_into_by_name_is_still_found(client, world, bundle):
    general = create_channel(world.owner, world.sid, "general")
    mark_imported(client, general["id"], "101")

    report = do_import(client, bundle, world.sid)

    row = {c.name: c for c in report.channels}["general"]
    assert (row.action, row.matched_by, row.channel_id) == (
        "existing", "earlier import", general["id"])
    assert len(messages_of(client, channel_id=general["id"])) == 7
    assert run(client, Channel.filter(server_id=world.sid, name="general").count()) == 1
    assert channel_marker(client, general["id"]) == {"source": SOURCE, "id": "101"}


def test_every_source_mapped_onto_one_channel_is_found_again(client, world, bundle):
    target = create_channel(world.owner, world.sid, "together")

    do_import(client, bundle, world.sid, only={"101", "106"},
              channel_map={"101": target["id"], "106": target["id"]})
    assert channel_marker(client, target["id"]) == {
        "source": SOURCE, "id": "106", "ids": ["106", "101"]}
    assert len(messages_of(client, channel_id=target["id"])) == 8
    count = run(client, Channel.filter(server_id=world.sid).count())

    again = do_import(client, bundle, world.sid, only={"101", "106"})

    assert {c.name: (c.action, c.channel_id) for c in again.channels if c.action != "skipped"} == {
        "general": ("existing", target["id"]), "off-topic": ("existing", target["id"])}
    assert again.messages == 0
    assert run(client, Channel.filter(server_id=world.sid).count()) == count


def test_a_source_mapped_later_is_added_to_the_marker(client, world, bundle):
    target = create_channel(world.owner, world.sid, "together")
    do_import(client, bundle, world.sid, only={"101"}, channel_map={"101": target["id"]})
    do_import(client, bundle, world.sid, only={"101", "106"}, channel_map={"106": target["id"]})
    assert channel_marker(client, target["id"])["ids"] == ["101", "106"]


def test_a_second_bundle_mapped_into_an_imported_channel_is_found_again(client, world, bundle, tmp_path):
    first = do_import(client, bundle, world.sid, only={"101"})
    general = next(c.channel_id for c in first.channels if c.name == "general")
    other = tmp_path / "other"
    shutil.copytree(bundle, other)
    edit_server_json(other, lambda data: data["source"].update(server_id="9001"))

    do_import(client, other, world.sid, only={"101"}, channel_map={"101": general})
    assert channel_marker(client, general)["sources"] == {"discord:9001": ["101"]}
    count = run(client, Channel.filter(server_id=world.sid).count())

    again = do_import(client, other, world.sid, only={"101"})

    row = next(c for c in again.channels if c.name == "general")
    assert (row.action, row.channel_id) == ("existing", general)
    assert run(client, Channel.filter(server_id=world.sid).count()) == count


def test_map_target_must_be_a_channel_of_the_same_type_in_this_server(client, world, bundle, new_client):
    voice = create_channel(world.owner, world.sid, "voice-one", "voice")
    with pytest.raises(ImportAborted, match="is a voice channel"):
        do_import(client, bundle, world.sid, dry_run=True, channel_map={"101": voice["id"]})
    other_owner = new_client()
    register(other_owner, "other-owner-bundle")
    other_server = create_server(other_owner, "Elsewhere")
    foreign = create_channel(other_owner, other_server["id"], "general")
    with pytest.raises(ImportAborted, match="no such channel"):
        do_import(client, bundle, world.sid, dry_run=True, channel_map={"101": foreign["id"]})
    with pytest.raises(ImportAborted, match="not in the bundle"):
        do_import(client, bundle, world.sid, dry_run=True, channel_map={"nope": voice["id"]})


def test_channel_from_an_earlier_run_is_found_even_when_renamed(client, world, bundle):
    do_import(client, bundle, world.sid)
    general = channel_named(client, world.sid, "general")
    run(client, Channel.filter(id=general.id).update(name="renamed"))
    create_channel(world.owner, world.sid, "general")

    dry = do_import(client, bundle, world.sid, dry_run=True)
    assert plans(dry)["general"] == "into existing #renamed"
    row = {c.name: c for c in dry.channels}["general"]
    assert (row.matched_by, row.name_taken) == ("earlier import", False)

    do_import(client, bundle, world.sid)
    assert run(client, Channel.filter(server_id=world.sid, name="general").count()) == 1
    assert run(client, Channel.filter(server_id=world.sid, name="renamed").count()) == 1


def test_a_category_with_the_same_name_is_reused(client, world, bundle):
    group = run(client, ChannelGroup.get(server_id=world.sid, name="Text channels"))

    do_import(client, bundle, world.sid)

    assert run(client, ChannelGroup.filter(server_id=world.sid, name="Text channels").count()) == 1
    assert channel_named(client, world.sid, "general").group_id == group.id


def test_only_limits_the_channels(client, world, bundle):
    report = do_import(client, bundle, world.sid, only={"106"})
    assert plans(report)["general"] == "skipped: not in --only"
    assert plans(report)["off-topic"] == "create"
    assert run(client, Channel.filter(server_id=world.sid).count()) == 1
    with pytest.raises(ImportAborted, match="not in the bundle"):
        do_import(client, bundle, world.sid, only={"999"})


def test_new_channels_go_to_the_end_of_their_category_in_source_order(client, world, bundle):
    existing = create_channel(world.owner, world.sid, "already-here")
    groups = {g.name: g for g in run(client, ChannelGroup.filter(server_id=world.sid))}
    run(client, Channel.filter(id=existing["id"]).update(
        group_id=groups["Text channels"].id, position=4))

    do_import(client, bundle, world.sid)
    general = channel_named(client, world.sid, "general")
    ideas = channel_named(client, world.sid, "ideas")
    assert (general.position, ideas.position) == (5, 6)
    assert channel_named(client, world.sid, "off-topic").position == 0


def test_a_missing_category_is_created(client, world, bundle):
    run(client, ChannelGroup.filter(server_id=world.sid, name="Voice channels").delete())
    do_import(client, bundle, world.sid)
    group = run(client, ChannelGroup.get(server_id=world.sid, name="Voice channels"))
    assert channel_named(client, world.sid, "Lounge").group_id == group.id
    assert group.position > 0


def test_forum_tags_match_by_name_and_stop_at_the_limit(client, world, bundle):
    ideas = create_channel(world.owner, world.sid, "ideas", "forum")
    mark_imported(client, ideas["id"], "105")
    run(client, ForumTag.create(channel_id=ideas["id"], name="idea"))
    edit_server_json(bundle, lambda data: data["channels"][2]["tags"].extend(
        {"id": f"x{i}", "name": f"extra-{i}"} for i in range(30)))

    report = do_import(client, bundle, world.sid)
    names = [t.name for t in run(client, ForumTag.filter(channel_id=ideas["id"]))]
    assert len(names) == 20 and names.count("idea") == 1
    assert report.tags_over_limit == 12


# Idempotence

def test_rerun_adds_nothing(client, world, bundle):
    first = do_import(client, bundle, world.sid, authors=authors(world))
    totals = [run(client, model.all().count()) for model in (Message, Attachment, ForumPost, Channel)]

    second = do_import(client, bundle, world.sid, authors=authors(world))
    assert [run(client, model.all().count()) for model in (Message, Attachment, ForumPost, Channel)] == totals
    assert second.messages == 0 and second.posts == 0 and second.attachments == 0
    assert sum(c.existing_messages for c in second.channels) == first.messages
    assert plans(second)["general"] == "into existing #general"
    assert {c.name: c.matched_by for c in second.channels}["general"] == "earlier import"
    dark = run(client, ForumPost.get(title="Dark mode?"))
    assert (dark.reply_count, dark.opening_message_id) == (2, message(client, "8101").id)


def test_refreshed_bundle_only_adds_what_is_new(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))
    chunk = bundle / "channels/101/messages/3.json"
    chunk.write_text(json.dumps([{
        "id": "1008", "author_id": "a1", "timestamp": "2024-03-09T09:00:00Z", "edited_at": None,
        "content": "A new message", "reply_to_id": "1007", "pinned": False,
        "attachments": [], "embeds": [], "reactions": []}]))
    thread = bundle / "channels/105/threads/8001.json"
    data = json.loads(thread.read_text())
    data["messages"].append({
        "id": "8104", "author_id": "a1", "timestamp": "2024-03-10T09:00:00Z", "edited_at": None,
        "content": "One more", "reply_to_id": None, "pinned": False,
        "attachments": [], "embeds": [], "reactions": []})
    thread.write_text(json.dumps(data))

    report = do_import(client, bundle, world.sid, authors=authors(world))
    assert report.messages == 2 and report.posts == 0
    assert message(client, "1008").reply_to_uuid == message(client, "1007").uuid
    dark = run(client, ForumPost.get(title="Dark mode?"))
    assert dark.reply_count == 3
    assert dark.last_activity_at == datetime(2024, 3, 10, 9, 0, tzinfo=timezone.utc)


def test_resume_after_a_failure_in_the_middle_of_a_channel(client, world, bundle, monkeypatch):
    monkeypatch.setattr(importer, "BATCH_SIZE", 2)
    original = Message.bulk_create
    calls = []

    async def failing(objects, *args, **kwargs):
        calls.append(len(objects))
        if len(calls) == 3:
            raise RuntimeError("disk on fire")
        return await original(objects, *args, **kwargs)

    monkeypatch.setattr(Message, "bulk_create", failing)
    with pytest.raises(RuntimeError):
        do_import(client, bundle, world.sid, authors=authors(world))
    stored = run(client, Message.all().count())
    assert 0 < stored < 13

    monkeypatch.setattr(Message, "bulk_create", original)
    report = do_import(client, bundle, world.sid, authors=authors(world))
    assert run(client, Message.all().count()) == 13
    assert len(set(run(client, Message.all().values_list("uuid", flat=True)))) == 13
    assert report.messages == 13 - stored
    assert run(client, ForumPost.all().count()) == 2
    assert run(client, ForumPost.get(title="Dark mode?")).reply_count == 2


def test_resume_does_not_leave_members_with_unread_imported_messages(client, world, bundle, monkeypatch):
    general = create_channel(world.owner, world.sid, "general")
    mark_imported(client, general["id"], "101")
    seed = run(client, Message.create(
        uuid=uuid.uuid4(), content="{}", author_id=world.alice_user["id"], server_id=world.sid,
        channel_id=general["id"], timestamp=datetime.now(timezone.utc)))
    run(client, ReadState.create(
        user_id=world.alice_user["id"], channel_id=general["id"], last_read_message_id=seed.id))
    monkeypatch.setattr(importer, "BATCH_SIZE", 2)
    original = Message.bulk_create
    calls = []

    async def failing(objects, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("stop")
        return await original(objects, *args, **kwargs)

    monkeypatch.setattr(Message, "bulk_create", failing)
    with pytest.raises(RuntimeError):
        do_import(client, bundle, world.sid, only={"101"})
    monkeypatch.setattr(Message, "bulk_create", original)
    do_import(client, bundle, world.sid, only={"101"})

    top = max(m.id for m in messages_of(client, channel_id=general["id"]))
    marker = run(client, ReadState.get(user_id=world.alice_user["id"], channel_id=general["id"]))
    assert marker.last_read_message_id == top


# Files

def test_oversized_file_is_skipped_and_counted(client, world, bundle, monkeypatch):
    (bundle / "files/9002/notes.txt").write_bytes(b"x" * 5000)
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "1000")

    report = do_import(client, bundle, world.sid)
    assert report.over_cap == 2
    assert sorted(report.over_cap_examples) == [
        "notes.txt (message 1006)", "report.pdf (message 1003)"]
    assert run(client, Attachment.filter(message_id=message(client, "1006").id).count()) == 0
    assert run(client, Attachment.filter(message_id=message(client, "1002").id).count()) == 1
    assert message(client, "1006")
    assert "Over the size limit" in format_report(report)


def test_an_absent_file_declared_over_the_limit_counts_as_over_it(client, world, bundle, monkeypatch):
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "100000")

    report = do_import(client, bundle, world.sid)
    assert report.over_cap == 1 and report.over_cap_examples == ["report.pdf (message 1003)"]
    assert report.missing == 0


def test_missing_file_is_skipped_and_counted(client, world, bundle):
    (bundle / "files/9001/cat.png").unlink()

    report = do_import(client, bundle, world.sid)
    assert report.missing == 2
    assert sorted(report.missing_examples) == [
        "cat.png (message 1002)", "report.pdf (message 1003)"]
    assert message(client, "1002")
    assert run(client, Attachment.all().count()) == 1


def test_a_bundle_path_cannot_leave_the_bundle(client, world, bundle, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    chunk = bundle / "channels/106/messages/1.json"
    data = json.loads(chunk.read_text())
    data[0]["attachments"] = [{"id": "1", "filename": "x.txt", "size": 6, "content_type": "text/plain",
                               "path": "../secret.txt"}]
    chunk.write_text(json.dumps(data))
    report = do_import(client, bundle, world.sid, only={"106"})
    assert report.missing == 1 and run(client, Attachment.all().count()) == 0


def test_a_message_that_ends_up_empty_is_skipped(client, world, bundle):
    chunk = bundle / "channels/106/messages/1.json"
    data = json.loads(chunk.read_text())
    data[0]["content"] = ""
    data[0]["attachments"] = [{"id": "1", "filename": "gone.txt", "size": 6, "content_type": "text/plain",
                               "path": None}]
    chunk.write_text(json.dumps(data))

    report = do_import(client, bundle, world.sid, only={"106"})
    assert report.empty_skipped == 1 and report.messages == 0 and report.missing == 1


def test_a_post_whose_opening_message_is_empty_still_imports(client, world, bundle):
    thread = bundle / "channels/105/threads/8001.json"
    data = json.loads(thread.read_text())
    data["messages"][0]["content"] = ""
    thread.write_text(json.dumps(data))

    do_import(client, bundle, world.sid)
    dark = run(client, ForumPost.get(title="Dark mode?"))
    assert dark.opening_message_id == message(client, "8101").id
    assert json.loads(message(client, "8101").content) == {"type": "doc", "content": []}


def test_a_thread_without_messages_becomes_a_post_without_an_opening_message(client, world, bundle):
    thread = bundle / "channels/105/threads/8002.json"
    data = json.loads(thread.read_text())
    data["messages"] = []
    thread.write_text(json.dumps(data))

    do_import(client, bundle, world.sid)
    cool = run(client, ForumPost.get(title="Look at this"))
    assert cool.opening_message_id is None and cool.reply_count == 0
    assert cool.last_activity_at == datetime(2024, 3, 7, 16, 0, tzinfo=timezone.utc)


# Authors

def test_mapped_and_unmapped_authors(client, world, bundle):
    report = do_import(client, bundle, world.sid, authors=authors(world))

    mine = message(client, "1001")
    assert mine.author_id == world.alice_user["id"]
    assert "imported_author" not in mine.metadata
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert system.password_hash == IMPORTED_PASSWORD_HASH and system.profile == {}
    bob = message(client, "1002")
    assert bob.author_id == system.id
    assert bob.metadata["imported_author"] == {"id": "a2", "name": "bob"}
    assert run(client, Attachment.get(message_id=bob.id)).uploader_id == system.id
    assert run(client, UserToServer.filter(user_id=system.id).count()) == 0
    unmapped_mention = json.loads(message(client, "1005").content)["content"][0]["content"]
    assert unmapped_mention[-1] == {"type": "text", "text": " cc @carol"}
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == system.id
    assert post.metadata["imported_author"] == {"id": "a4", "name": "dave"}
    assert [(a.id, a.name, a.messages) for a in report.unmapped_authors] == [
        ("a2", "bob", 3), ("a3", "carol", 3), ("a4", "dave", 3)]
    assert "Authors with no mapping" in format_report(report)


def test_an_author_missing_from_the_bundle_is_named_by_id(client, world, bundle):
    edit_server_json(bundle, lambda data: data.__setitem__(
        "authors", [a for a in data["authors"] if a["id"] != "a4"]))
    report = do_import(client, bundle, world.sid, only={"106"})
    assert message(client, "6001").metadata["imported_author"] == {"id": "a4", "name": "a4"}
    assert report.unmapped_authors[0].name == "a4"


def test_unknown_username_aborts_before_anything_is_written(client, world, bundle):
    for dry_run in (True, False):
        with pytest.raises(ImportAborted, match="ghost"):
            do_import(client, bundle, world.sid, authors={"a1": "ghost"}, dry_run=dry_run)
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0
    assert run(client, User.filter(username=IMPORTED_USERNAME).exists()) is False


def test_a_mapped_user_outside_the_server_is_a_warning(client, world, bundle):
    report = do_import(client, bundle, world.sid, authors=authors(
        world, a2=world.carol_user["username"]), only={"101"})
    assert report.warnings == [
        f"{world.carol_user['username']} is mapped but is not a member of the server"]
    assert message(client, "1002").author_id == world.carol_user["id"]


def test_the_import_account_cannot_be_mapped_or_replaced(client, world, bundle):
    with pytest.raises(ImportAborted, match="can't be mapped"):
        do_import(client, bundle, world.sid, authors={"a1": IMPORTED_USERNAME})
    run(client, User.create(username=IMPORTED_USERNAME, password_hash="$2b$12$abc"))
    with pytest.raises(ImportAborted, match="not the import account"):
        do_import(client, bundle, world.sid, dry_run=True)


def test_rerun_hands_messages_to_newly_mapped_users(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))
    system = run(client, User.get(username=IMPORTED_USERNAME))
    owner_id = user_id(client, "owner-bundle")

    report = do_import(client, bundle, world.sid, authors=authors(
        world, a2=world.bob_user["username"], a4="owner-bundle"))

    assert sum(c.handed_over for c in report.channels) == 6
    handed = message(client, "1002")
    assert handed.author_id == world.bob_user["id"]
    assert "imported_author" not in handed.metadata
    assert handed.metadata["import"] == {"source": SOURCE, "id": "1002"}
    assert run(client, Attachment.get(message_id=handed.id)).uploader_id == world.bob_user["id"]
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == owner_id and "imported_author" not in post.metadata
    assert message(client, "8201").author_id == owner_id
    assert message(client, "1003").author_id == system.id
    assert run(client, Message.all().count()) == 13

    do_import(client, bundle, world.sid, authors=authors(world))
    back = message(client, "1002")
    assert back.author_id == system.id
    assert back.metadata["imported_author"] == {"id": "a2", "name": "bob"}
    assert run(client, ForumPost.get(title="Look at this")).metadata["imported_author"] == {
        "id": "a4", "name": "dave"}


def model_counts(client):
    return [run(client, model.all().count()) for model in (
        Channel, ChannelGroup, Message, Attachment, ForumPost, ForumTag, User, ReadState)]


def test_existing_only_hands_over_without_creating_or_reading_files(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))
    shutil.rmtree(bundle / "files")
    before = model_counts(client)
    owner_id = user_id(client, "owner-bundle")

    report = do_import(client, bundle, world.sid, existing_only=True, authors=authors(
        world, a2=world.bob_user["username"], a4="owner-bundle"))

    assert model_counts(client) == before
    assert sum(c.handed_over for c in report.channels) == 6
    assert message(client, "1002").author_id == world.bob_user["id"]
    assert run(client, ForumPost.get(title="Look at this")).author_id == owner_id
    assert report.missing == 0 and report.over_cap == 0
    assert all(count == 0 for count in vars(report.left_out).values())
    assert (report.empty_skipped, report.invalid_skipped, report.tags_over_limit) == (0, 0, 0)
    assert (report.messages, report.posts, report.attachments) == (0, 0, 0)


def test_existing_only_on_a_server_without_the_import_does_nothing(client, world, bundle):
    create_channel(client, world.sid, "general")
    before = model_counts(client)

    report = do_import(client, bundle, world.sid, existing_only=True, authors=authors(world))

    assert model_counts(client) == before
    assert {c.name: (c.action, c.reason) for c in report.channels}["ideas"] == (
        "skipped", "not imported")
    assert sum(c.handed_over + c.existing_messages for c in report.channels) == 0


def test_progress_is_reported_after_every_batch_and_matches_a_dry_run(
        client, world, bundle, monkeypatch):
    monkeypatch.setattr(importer, "BATCH_SIZE", 2)
    seen = []

    async def progress(count, channel):
        seen.append((count, channel))

    dry = run(client, import_bundle(
        bundle, ImportOptions(server_id=world.sid, dry_run=True), progress))
    assert seen and seen == sorted(seen)
    assert seen[-1][0] == dry.seen > 0
    assert {name for _, name in seen} <= {"general", "ideas", "off-topic"}

    seen.clear()
    real = run(client, import_bundle(bundle, ImportOptions(server_id=world.sid), progress))
    assert real.seen == dry.seen and seen[-1][0] == real.seen
    assert len(seen) > 3


def test_attachments_are_sniffed_off_the_event_loop(client, world, bundle, monkeypatch):
    threads = []
    real = importer.sniff_image

    def spy(data):
        threads.append(threading.current_thread())
        return real(data)

    monkeypatch.setattr(importer, "sniff_image", spy)
    do_import(client, bundle, world.sid)
    assert threads and all(t is not threading.main_thread() for t in threads)


def test_the_report_lists_every_author_seen(client, world, bundle):
    report = do_import(client, bundle, world.sid, authors=authors(world))

    assert [(a.id, a.name, a.messages) for a in report.authors] == sorted(
        [(a.id, a.name, a.messages) for a in report.authors], key=lambda a: (-a[2], a[1]))
    assert {a.id for a in report.authors} == {"a1", "a2", "a3", "a4"}
    assert [a.id for a in report.unmapped_authors] == [
        a.id for a in report.authors if a.id != "a1"]


def test_channels_say_what_will_happen_and_why_not(client, world, bundle):
    mark_imported(client, create_channel(client, world.sid, "general")["id"], "101")
    edit_server_json(bundle, lambda data: data["channels"][3].update(type="stage"))

    report = do_import(client, bundle, world.sid, dry_run=True)
    by_name = {c.name: c for c in report.channels}
    assert (by_name["general"].action, by_name["general"].target_name) == ("existing", "general")
    assert (by_name["ideas"].action, by_name["ideas"].target_name) == ("create", None)
    assert (by_name["staff"].action, by_name["staff"].reason) == ("skipped", "private")
    assert (by_name["archive"].action, by_name["archive"].reason) == ("skipped", "unreadable")
    assert by_name["Lounge"].reason == "unsupported type stage"
    assert plans(report)["staff"] == "skipped: private"

    only = do_import(client, bundle, world.sid, dry_run=True, only={"101"})
    assert {c.name: c.reason for c in only.channels}["ideas"] == "not selected"
    assert plans(only)["ideas"] == "skipped: not in --only"


def test_the_plan_is_the_reports_json_form(client, world, bundle):
    report = do_import(client, bundle, world.sid, authors=authors(world), dry_run=True)
    plan = plan_json(report)

    assert json.loads(json.dumps(plan)) == plan
    assert set(plan) == {
        "channels", "totals", "free_bytes", "over_cap", "missing", "left_out", "warnings"}
    assert plan["totals"] == {
        "messages": 13, "existing_messages": 0, "posts": 2,
        "attachments": report.attachments, "attachment_bytes": report.attachment_bytes}
    assert set(plan["channels"][0]) == {
        "source_id", "name", "type", "action", "target_name", "name_taken", "reason", "category",
        "messages", "existing_messages", "posts", "existing_posts", "attachments", "attachment_bytes",
        "handed_over", "private", "private_action", "visibility"}
    assert plan["left_out"]["avatars"] == 2 and plan["left_out"]["private_channels"] == 1
    assert plan["missing"] == 1
    assert set(plan["left_out"]) == {
        "private_channels", "unreadable_channels", "text_channel_threads",
        "messages_with_reactions", "pinned_messages", "voice_text_chat", "emoji", "avatars",
        "over_attachment_limit", "tags_over_limit", "empty_messages", "invalid_messages"}


def test_the_same_bundle_imported_into_two_servers_gives_two_independent_imports(
        client, world, bundle, new_client):
    other_owner = new_client()
    register(other_owner, "second-owner")
    other = create_server(other_owner, "Second")
    do_import(client, bundle, world.sid, authors=authors(world))
    first = [(m.id, m.uuid, m.author_id, m.metadata) for m in messages_of(client, server_id=world.sid)]

    report = do_import(client, bundle, other["id"], authors={"a2": "second-owner"})

    assert report.messages == 13 and report.warnings == []
    assert run(client, Message.filter(server_id=other["id"]).count()) == 13
    assert [(m.id, m.uuid, m.author_id, m.metadata)
            for m in messages_of(client, server_id=world.sid)] == first
    assert message(client, "1002", other["id"]).uuid != message(client, "1002", world.sid).uuid
    assert message(client, "1002", other["id"]).reply_to_uuid == message(
        client, "1001", other["id"]).uuid

    do_import(client, bundle, other["id"], existing_only=True, authors={
        "a1": world.alice_user["username"], "a2": "second-owner"})

    assert [(m.id, m.uuid, m.author_id, m.metadata)
            for m in messages_of(client, server_id=world.sid)] == first
    assert message(client, "1001", other["id"]).author_id == world.alice_user["id"]
    assert run(client, Message.filter(server_id=world.sid).count()) == 13


def test_existing_only_creates_the_import_account_when_an_author_is_unmapped_later(
        client, world, bundle):
    everyone = {"a1": world.alice_user["username"], "a2": world.bob_user["username"],
                "a3": world.alice_user["username"], "a4": world.bob_user["username"]}
    do_import(client, bundle, world.sid, authors=everyone)
    assert run(client, User.filter(username=IMPORTED_USERNAME).exists()) is False

    do_import(client, bundle, world.sid, existing_only=True, authors={
        k: v for k, v in everyone.items() if k not in ("a2", "a4")})

    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert message(client, "1002").author_id == system.id
    assert message(client, "1002").metadata["imported_author"] == {"id": "a2", "name": "bob"}
    post = run(client, ForumPost.get(title="Look at this"))
    assert post.author_id == system.id and post.metadata["imported_author"]["id"] == "a4"


# Private channels, formats

def test_private_channel_skipped_and_included(client, world, bundle):
    skipped = do_import(client, bundle, world.sid, only={"101", "103"})
    assert plans(skipped)["staff"] == "skipped: private"
    assert skipped.left_out.private_channels == 1
    assert run(client, Channel.filter(server_id=world.sid, name="staff").exists()) is False

    included = do_import(
        client, bundle, world.sid, only={"101", "103"}, private={"103": "everyone"})
    assert plans(included)["staff"] == "create, everyone can see it"
    staff = channel_named(client, world.sid, "staff")
    assert len(messages_of(client, channel_id=staff.id)) == 1
    assert included.left_out.private_channels == 0


def overwrites_of(client, channel_id):
    rows = run(client, PermissionOverwrite.filter(channel_id=channel_id))
    return sorted((r.role_id, r.user_id, r.allow, r.deny) for r in rows)


def everyone_role(client, sid):
    return run(client, Role.get(server_id=sid, is_default=True))


def test_only_me_creates_one_overwrite_hiding_the_channel_from_everyone(client, world, bundle):
    report = do_import(client, bundle, world.sid, only={"103"}, private={"103": "only_me"})

    staff = channel_named(client, world.sid, "staff")
    assert overwrites_of(client, staff.id) == [
        (everyone_role(client, world.sid).id, None, 0, int(Permission.VIEW_CHANNEL))]
    assert run(client, PermissionOverwrite.filter(server_id=world.sid).count()) == 1
    row = next(c for c in report.channels if c.source_id == "103")
    assert (row.private, row.visibility, row.action) == (True, "only_me", "create")
    assert row.plan == "create, only the owner can see it"
    assert row.channel_id == staff.id and row.messages == 1


def test_everyone_creates_no_overwrite(client, world, bundle):
    do_import(client, bundle, world.sid, only={"103"}, private={"103": "everyone"})

    staff = channel_named(client, world.sid, "staff")
    assert overwrites_of(client, staff.id) == []
    assert run(client, PermissionOverwrite.filter(server_id=world.sid).count()) == 0


def test_a_dry_run_with_private_channels_writes_no_overwrite(client, world, bundle):
    report = do_import(
        client, bundle, world.sid, private={"103": "only_me"}, dry_run=True)

    staff = next(c for c in report.channels if c.source_id == "103")
    assert (staff.private, staff.visibility, staff.messages) == (True, "only_me", 1)
    assert run(client, PermissionOverwrite.filter(server_id=world.sid).count()) == 0
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0


def test_public_rows_are_not_private(client, world, bundle):
    report = do_import(client, bundle, world.sid, dry_run=True)
    assert {c.source_id: c.private for c in report.channels} == {
        "101": False, "103": True, "105": False, "104": False, "106": False, "107": False}
    assert all(c.visibility is None for c in report.channels)


def test_a_rerun_leaves_the_owners_edits_to_the_overwrites_alone(client, world, bundle):
    do_import(client, bundle, world.sid, only={"103"}, private={"103": "only_me"})
    staff = channel_named(client, world.sid, "staff")
    run(client, PermissionOverwrite.filter(channel_id=staff.id).delete())
    run(client, PermissionOverwrite.create(
        server_id=world.sid, channel_id=staff.id, user_id=world.alice_user["id"],
        allow=int(Permission.VIEW_CHANNEL), deny=0))
    edited = overwrites_of(client, staff.id)

    for visibility in ("only_me", "everyone"):
        report = do_import(
            client, bundle, world.sid, only={"103"}, private={"103": visibility})
        row = next(c for c in report.channels if c.source_id == "103")
        assert (row.action, row.visibility, row.private) == ("existing", None, True)
        assert overwrites_of(client, staff.id) == edited
    assert run(client, Channel.filter(server_id=world.sid, name="staff").count()) == 1


def test_a_private_channel_that_is_not_selected_is_skipped_and_counted(client, world, bundle):
    report = do_import(client, bundle, world.sid)
    staff = next(c for c in report.channels if c.source_id == "103")
    assert (staff.action, staff.reason, staff.private, staff.visibility) == (
        "skipped", "private", True, None)
    assert report.left_out.private_channels == 1


@pytest.mark.parametrize("private, message", [
    ({"999": "only_me"}, "Channel 999 is not in the bundle"),
    ({"101": "only_me"}, "Channel 101 is not private"),
])
def test_private_options_must_name_private_channels_of_the_bundle(
        client, world, bundle, private, message):
    with pytest.raises(ImportAborted, match=message):
        do_import(client, bundle, world.sid, private=private)
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0


def test_a_private_channel_is_never_merged_into_an_existing_one(client, world, bundle):
    target = create_channel(client, world.sid, "staff-room")
    with pytest.raises(ImportAborted, match="--map 103=.*never merged"):
        do_import(
            client, bundle, world.sid, channel_map={"103": target["id"]},
            private={"103": "only_me"})
    assert run(client, Message.filter(channel_id=target["id"]).count()) == 0


def test_a_missing_everyone_role_aborts_before_anything_is_written(client, world, bundle):
    run(client, Role.filter(server_id=world.sid, is_default=True).update(is_default=False))
    with pytest.raises(ImportAborted, match="no @everyone role"):
        do_import(client, bundle, world.sid, private={"103": "only_me"})
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0
    do_import(client, bundle, world.sid, private={"103": "everyone"})


def test_existing_only_hands_over_messages_in_an_imported_private_channel(client, world, bundle):
    do_import(client, bundle, world.sid, only={"103"}, private={"103": "only_me"})
    staff = channel_named(client, world.sid, "staff")
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert messages_of(client, channel_id=staff.id)[0].author_id == system.id

    report = do_import(
        client, bundle, world.sid, authors=authors(world), existing_only=True)

    row = next(c for c in report.channels if c.source_id == "103")
    assert row.handed_over == 1 and row.private is True
    assert messages_of(client, channel_id=staff.id)[0].author_id == world.alice_user["id"]


def test_existing_only_does_not_create_private_channels(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world), existing_only=True)
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0


def test_messages_seen_per_channel_add_up_to_the_total(client, world, bundle):
    report = do_import(
        client, bundle, world.sid, authors=authors(world), private={"103": "only_me"})
    assert sum(c.seen for c in report.channels) == report.seen
    assert next(c for c in report.channels if c.source_id == "103").seen == 1

    again = do_import(
        client, bundle, world.sid, authors=authors(world), private={"103": "only_me"})
    assert sum(c.seen for c in again.channels) == again.seen == report.seen


# Command line

def cli_args(bundle, *flags):
    return import_cli.parse_args([str(bundle), "--server", "1", *flags])


def test_the_command_line_leaves_private_channels_out_by_default(bundle):
    assert import_cli.build_options(cli_args(bundle)).private == {}


def test_include_private_makes_every_private_channel_only_me(bundle):
    options = import_cli.build_options(cli_args(bundle, "--include-private"))
    assert options.private == {"103": "only_me"}


def test_private_visible_makes_them_visible_to_everyone(bundle):
    options = import_cli.build_options(
        cli_args(bundle, "--include-private", "--private-visible"))
    assert options.private == {"103": "everyone"}


def test_private_visible_needs_include_private(bundle, capsys):
    with pytest.raises(SystemExit):
        cli_args(bundle, "--private-visible")
    assert "--include-private" in capsys.readouterr().err


def test_unknown_format_version_is_refused(client, world, bundle):
    edit_server_json(bundle, lambda data: data.__setitem__("format", 2))
    with pytest.raises(BundleError, match="Unsupported bundle format 2"):
        do_import(client, bundle, world.sid, dry_run=True)
    assert run(client, Channel.filter(server_id=world.sid).count()) == 0


def test_unknown_server_and_missing_bundle(client, world, bundle, tmp_path):
    with pytest.raises(ImportAborted, match="No server"):
        do_import(client, bundle, 9999, dry_run=True)
    with pytest.raises(BundleError, match="missing"):
        do_import(client, tmp_path / "empty", world.sid, dry_run=True)


def test_chunks_are_read_in_numeric_order_one_at_a_time(bundle):
    chunks = bundle / "channels/106/messages"
    for number in (2, 10):
        (chunks / f"{number}.json").write_text(json.dumps([{
            "id": f"n{number}", "author_id": "a1", "timestamp": "2024-03-05T00:00:00Z",
            "content": "x"}]))
    iterator = iter_channel_messages(bundle, "106")
    assert [chunk[0].id for chunk in iterator] == ["6001", "n2", "n10"]
    assert load_server(bundle).source.server_id == "9000"


# Unread

def test_members_who_were_caught_up_stay_caught_up(client, world, bundle):
    general = create_channel(world.owner, world.sid, "general")
    mark_imported(client, general["id"], "101")
    seed = run(client, Message.create(
        uuid=uuid.uuid4(), content="{}", author_id=world.alice_user["id"], server_id=world.sid,
        channel_id=general["id"], timestamp=datetime.now(timezone.utc)))
    run(client, ReadState.create(
        user_id=world.alice_user["id"], channel_id=general["id"], last_read_message_id=seed.id))
    run(client, ReadState.create(
        user_id=world.bob_user["id"], channel_id=general["id"], last_read_message_id=seed.id - 1))

    do_import(client, bundle, world.sid)

    top = max(m.id for m in messages_of(client, channel_id=general["id"]))
    assert top > seed.id
    def marker(user):
        return run(client, ReadState.get(user_id=user["id"], channel_id=general["id"])).last_read_message_id
    assert marker(world.alice_user) == top
    assert marker(world.bob_user) == seed.id - 1
    state = {c["name"]: c for c in world.alice.get(f"/channels/{world.sid}").json()}
    assert state["general"]["last_read_message_id"] == state["general"]["last_message_id"]


def test_channels_the_import_created_are_read_for_everyone(client, world, bundle):
    do_import(client, bundle, world.sid)

    for member in (world.owner, world.alice, world.bob):
        channels = {c["name"]: c for c in member.get(f"/channels/{world.sid}").json()}
        for name in ("general", "off-topic"):
            assert channels[name]["last_message_id"]
            assert channels[name]["last_read_message_id"] == channels[name]["last_message_id"]
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert run(client, ReadState.filter(user_id=system.id).count()) == 0


def test_the_import_does_not_notify_anyone(client, world, bundle, monkeypatch):
    from app.services import read_state
    from app.services.push import push

    async def forbidden(*args, **kwargs):
        raise AssertionError("the import must not use read_state.advance")

    monkeypatch.setattr(read_state, "advance", forbidden)
    monkeypatch.setattr(push, "notify_read", lambda *a, **k: pytest.fail("push"))
    with world.alice.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        do_import(client, bundle, world.sid)
        ws.send_json({"type": "ping", "t": 1.0})
        frames = []
        while True:
            frame = ws.receive_json()
            if frame["type"] == "pong":
                break
            frames.append(frame)
    assert [f for f in frames if f["type"] != "presence_update"] == []


# Wire contract

def history(client, channel_id, **params):
    res = client.get(f"/channels/{channel_id}/messages", params=params)
    assert res.status_code == 200, res.text
    return {m["id"]: m for m in res.json()["messages"]}


def test_history_carries_imported_author(client, world, bundle):
    do_import(client, bundle, world.sid, authors=authors(world))
    general = channel_named(client, world.sid, "general")

    rows = history(world.alice, general.id)
    assert rows[str(message_uuid(world.sid, SOURCE, "1001"))]["imported_author"] is None
    assert rows[str(message_uuid(world.sid, SOURCE, "1002"))]["imported_author"] == {"id": "a2", "name": "bob"}
    quote = rows[str(message_uuid(world.sid, SOURCE, "1002"))]["reply_to"]
    assert quote["imported_author"] is None and quote["user_id"] == world.alice_user["id"]
    system = run(client, User.get(username=IMPORTED_USERNAME))
    assert rows[str(message_uuid(world.sid, SOURCE, "1002"))]["user_id"] == system.id
    assert rows[str(message_uuid(world.sid, SOURCE, "1002"))]["timestamp"].startswith("2024-03-01T10:05:00")


def test_forum_history_and_quotes_carry_imported_author(client, world, bundle):
    do_import(client, bundle, world.sid, authors={"a2": world.alice_user["username"]})
    ideas = channel_named(client, world.sid, "ideas")
    dark = run(client, ForumPost.get(title="Dark mode?"))

    rows = history(world.alice, ideas.id, post_id=dark.id)
    assert list(rows) == [str(message_uuid(world.sid, SOURCE, m)) for m in ("8103", "8102", "8101")]
    reply = rows[str(message_uuid(world.sid, SOURCE, "8102"))]
    assert reply["reply_to"]["imported_author"] == {"id": "a1", "name": "alice"}
    assert reply["imported_author"] is None


def test_live_messages_have_a_null_imported_author(client, world):
    general = create_channel(world.owner, world.sid, "live")
    frame = {"type": "message", "id": str(uuid.uuid4()), "server_id": world.sid,
             "channel_id": general["id"], "timestamp": datetime.now(timezone.utc).isoformat(),
             "content": "hi"}
    with world.alice.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            world.bob.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.send_json(frame)
        while (live := bob_ws.receive_json())["type"] != "message":
            pass
    assert live["imported_author"] is None
    assert history(world.alice, general["id"])[frame["id"]]["imported_author"] is None
    res = world.alice.patch(f"/messages/{frame['id']}", json={"content": "edited"})
    assert res.status_code == 200 and res.json()["imported_author"] is None


def test_a_client_cannot_set_imported_author(client, world):
    general = create_channel(world.owner, world.sid, "forge")
    forged = {"id": "x", "name": "Someone else"}
    frame = {"type": "message", "id": str(uuid.uuid4()), "server_id": world.sid,
             "channel_id": general["id"], "timestamp": datetime.now(timezone.utc).isoformat(),
             "content": "hi", "metadata": {
                 "imported_author": forged, "import": {"source": SOURCE, "id": "1"}, "kept": 1}}
    with world.alice.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(frame)
        while ws.receive_json()["type"] != "message_ack":
            pass
    stored = run(client, Message.get(uuid=uuid.UUID(frame["id"])))
    assert stored.metadata == {"kept": 1}
    assert history(world.alice, general["id"])[frame["id"]]["imported_author"] is None


def test_direct_message_metadata_is_stripped_too():
    frame = DirectMessageFrame(
        type="direct_message", id="x", conversation_id=1, content="hi", timestamp="t",
        metadata={"imported_author": {"id": "1", "name": "n"}, "import": {}, "kept": 2})
    assert ChatService._stored_metadata(frame) == {"kept": 2}
    channel_frame = MessageFrame(
        type="message", id="x", server_id=1, channel_id=1, content="hi", timestamp="t",
        metadata={"imported_author": 1, "kept": 3})
    assert ChatService._stored_metadata(channel_frame) == {"kept": 3}


# The import account

def test_import_account_cannot_log_in(client, world, bundle, new_client):
    do_import(client, bundle, world.sid)
    for password in (PASSWORD, "!", ""):
        res = new_client().post(
            "/auth/login", json={"username": IMPORTED_USERNAME, "password": password})
        assert res.status_code == 401
        assert res.json()["detail"] == "Incorrect username or password"
    wrong = new_client().post(
        "/auth/login", json={"username": world.alice_user["username"], "password": "nope nope nope"})
    assert wrong.json() == res.json()


def test_import_account_is_left_out_of_user_lists_and_counts(client, world, bundle, new_client):
    admin_client = new_client()
    make_admin(admin_client, "bundle-admin")
    before = admin_client.get("/admin/stats").json()["users"]

    do_import(client, bundle, world.sid)
    system = run(client, User.get(username=IMPORTED_USERNAME))
    run(client, User.filter(id=system.id).update(last_active_at=datetime.now(timezone.utc)))

    assert IMPORTED_USERNAME not in [u["username"] for u in world.alice.get("/users/").json()]
    after = admin_client.get("/admin/stats").json()["users"]
    for key in ("total", "new_7d", "dau", "wau"):
        assert after[key] == before[key]


def test_no_conversation_can_be_opened_with_the_import_account(client, world, bundle):
    do_import(client, bundle, world.sid)
    system = run(client, User.get(username=IMPORTED_USERNAME))
    res = world.alice.post("/conversations/", json={"user_id": system.id})
    assert res.status_code == 404
