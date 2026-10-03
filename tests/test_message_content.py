"""Message content is stored as a validated JSON TipTap doc.

Covers the normaliser itself, the chat / DM / edit write paths and the data
migration that rewrote older rows.
"""
import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tortoise import connections

from app.models.Message import Message
from app.services.message_content import (
    MAX_CONTENT_DEPTH,
    MAX_CONTENT_NODES,
    InvalidContent,
    normalize_content,
    serialize,
)
from tests.conftest import ORIGIN, register, ws_ready
from tests.test_message_edit_delete import (
    DOC,
    EDITED,
    chat_frame,
    post_message,
    setup_server_with_two_members,
)

HEADERS = {"origin": ORIGIN}


def text_doc(*lines):
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line
        else {"type": "paragraph"}
        for line in lines
    ]}


def para(*children):
    return {"type": "paragraph", "content": list(children)}


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def nested(depth):
    node = para(text("x"))
    for _ in range(depth):
        node = {"type": "blockquote", "content": [node]}
    return doc(node)


# --- normaliser --------------------------------------------------------------

def test_accepts_every_supported_node_and_mark():
    mention = {"type": "mention", "attrs": {"id": "7", "label": "bob"}}
    raw = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("title")]},
        para(text("a", "bold", "italic", "strike", "code", "underline"), mention,
             {"type": "hardBreak"},
             {"type": "text", "text": "link", "marks": [
                 {"type": "link", "attrs": {"href": "https://example.com", "target": None}}]}),
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [para(text("one"))]}]},
        {"type": "orderedList", "attrs": {"start": 1}, "content": [
            {"type": "listItem", "content": [para(text("two"))]}]},
        {"type": "blockquote", "content": [para(text("quoted"))]},
        {"type": "codeBlock", "attrs": {"language": None}, "content": [text("x = 1")]},
        {"type": "horizontalRule"},
    )
    assert normalize_content(raw, allow_empty=False) == raw


def test_string_becomes_one_paragraph_per_line():
    assert normalize_content("hello\n\nworld", allow_empty=False) == text_doc("hello", "", "world")


def test_nodes_with_unset_fields_are_dumped_without_them():
    raw = doc(para(text("hi")))
    assert normalize_content(raw, allow_empty=False) == raw


@pytest.mark.parametrize("raw", [
    doc(para(text("hi")), {"type": "script"}),
    doc(para({"type": "text", "text": "hi", "marks": [{"type": "sparkle"}]})),
    {**doc(para(text("hi"))), "extra": 1},
    doc({"type": "paragraph", "content": [text("hi")], "style": "x"}),
    para(text("hi")),
    [1, 2],
    42,
    True,
    {"type": "doc", "content": "hi"},
    doc(para({"type": "text", "text": ""})),
    doc(para({"type": "text"})),
    doc({"type": "paragraph", "text": "stray"}),
    doc(para({"type": "text", "text": "hi", "content": []})),
    doc({"type": "paragraph", "marks": [{"type": "bold"}]}),
    doc({"type": "heading", "attrs": {"level": {"nested": 1}}, "content": [text("a")]}),
])
def test_rejects_unsupported_content(raw):
    with pytest.raises(InvalidContent):
        normalize_content(raw, allow_empty=True)


def test_rejects_too_deep_content():
    assert normalize_content(nested(MAX_CONTENT_DEPTH - 3), allow_empty=False)
    with pytest.raises(InvalidContent):
        normalize_content(nested(MAX_CONTENT_DEPTH + 1), allow_empty=False)


def test_rejects_hostile_depth_without_recursing():
    with pytest.raises(InvalidContent):
        normalize_content(nested(50_000), allow_empty=False)


def test_rejects_too_many_nodes():
    raw = doc(*[para(text("x")) for _ in range(MAX_CONTENT_NODES)])
    with pytest.raises(InvalidContent):
        normalize_content(raw, allow_empty=False)


@pytest.mark.parametrize("raw", [
    None,
    "",
    "  \n ",
    {"type": "doc"},
    {"type": "doc", "content": []},
    doc(para(), para()),
])
def test_empty_content_needs_attachments(raw):
    with pytest.raises(InvalidContent):
        normalize_content(raw, allow_empty=False)
    assert normalize_content(raw, allow_empty=True) == {"type": "doc", "content": []}


def test_content_with_only_a_mention_break_or_rule_is_not_empty():
    mention = doc(para({"type": "mention", "attrs": {"id": "1"}}))
    assert normalize_content(mention, allow_empty=False) == mention
    breaks = doc(para({"type": "hardBreak"}))
    assert normalize_content(breaks, allow_empty=False) == breaks
    rule = doc({"type": "horizontalRule"})
    assert normalize_content(rule, allow_empty=False) == rule


def test_empty_doc_result_is_not_shared():
    first = normalize_content(None, allow_empty=True)
    first["content"].append("junk")
    assert normalize_content(None, allow_empty=True) == {"type": "doc", "content": []}


def test_serialize_is_compact_json():
    assert serialize(text_doc("é")) == (
        '{"type":"doc","content":[{"type":"paragraph",'
        '"content":[{"type":"text","text":"é"}]}]}')


# --- write paths -------------------------------------------------------------

def recv(ws, wanted_type):
    """Next frame of *wanted_type*, skipping presence noise."""
    for _ in range(10):
        frame = ws.receive_json()
        if frame["type"] == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame arrived")


def history(client, channel_id):
    return client.get(f"/channels/{channel_id}/messages").json()["messages"]


def test_channel_message_is_stored_and_broadcast_normalised(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online

        message_id = str(uuid.uuid4())
        frame = {**chat_frame(server["id"], channel["id"], message_id), "content": "a\nb"}
        alice_ws.send_json(frame)
        received = bob_ws.receive_json()
        assert received["content"] == text_doc("a", "b")

    stored = history(client, channel["id"])[0]["content"]
    assert stored == serialize(text_doc("a", "b"))


def test_channel_message_with_invalid_content_is_rejected(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws:
        ws_ready(alice_ws)
        for content in ({**DOC, "forged": True}, [1], "", None):
            frame = {**chat_frame(server["id"], channel["id"], str(uuid.uuid4())),
                     "content": content}
            alice_ws.send_json(frame)
            error = alice_ws.receive_json()
            assert error == {"type": "error", "code": "invalid_content", "ref": frame["id"]}
    assert history(client, channel["id"]) == []


def test_direct_message_is_normalised_and_invalid_content_rejected(client, new_client):
    register(client, "alice-mc")
    bob_client = new_client()
    bob = register(bob_client, "bob-mc")
    convo = client.post("/conversations/", json={"user_id": bob["id"]}).json()

    def dm(content):
        return {
            "type": "direct_message",
            "id": str(uuid.uuid4()),
            "conversation_id": convo["id"],
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)

        bad = dm({**DOC, "forged": True})
        alice_ws.send_json(bad)
        error = recv(alice_ws, "error")
        assert error == {"type": "error", "code": "invalid_content", "ref": bad["id"]}

        good = dm("hi")
        alice_ws.send_json(good)
        received = recv(bob_ws, "direct_message")
        assert received["type"] == "direct_message"
        assert received["content"] == text_doc("hi")

    messages = client.get(f"/conversations/{convo['id']}/messages").json()["messages"]
    assert [m["id"] for m in messages] == [good["id"]]
    assert messages[0]["content"] == serialize(text_doc("hi"))


def test_edit_stores_and_returns_normalised_content(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online
        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        res = client.patch(f"/messages/{message_id}", json={"content": "edited"})
        assert res.status_code == 200, res.text
        assert res.json()["content"] == text_doc("edited")
        assert bob_ws.receive_json()["content"] == text_doc("edited")

        res = client.patch(f"/messages/{message_id}", json={"content": EDITED})
        assert res.json()["content"] == EDITED

    assert history(client, channel["id"])[0]["content"] == serialize(EDITED)


def test_edit_with_invalid_content_is_422_and_changes_nothing(client, new_client):
    server, channel, bob_client = setup_server_with_two_members(client, new_client)
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()  # bob online
        message_id = post_message(alice_ws, bob_ws, server["id"], channel["id"])

        for content in ({"type": "doc", "content": [{"type": "script"}]}, 5, "", None):
            res = client.patch(f"/messages/{message_id}", json={"content": content})
            assert res.status_code == 422, res.text
            assert res.json() == {"detail": "Invalid message content"}

    assert history(client, channel["id"])[0]["content"] == serialize(DOC)


# --- data migration ----------------------------------------------------------

def load_migration():
    path = next(Path(__file__).parent.parent.glob("migrations/models/32_*_normalize_message_content.py"))
    spec = importlib.util.spec_from_file_location("normalize_message_content", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load_migration()


def test_migration_keeps_json_docs_unchanged():
    raw = serialize(DOC)
    assert migration.normalized(raw) == raw


def test_migration_compacts_spaced_json_docs():
    assert migration.normalized(json.dumps(DOC)) == serialize(DOC)


def test_migration_converts_python_repr_docs():
    raw = repr({"type": "doc", "content": [
        {"type": "paragraph", "attrs": {"flag": True, "none": None},
         "content": [{"type": "text", "text": "it's"}]}]})
    assert "True" in raw and "None" in raw
    assert json.loads(migration.normalized(raw)) == {
        "type": "doc", "content": [
            {"type": "paragraph", "attrs": {"flag": True, "none": None},
             "content": [{"type": "text", "text": "it's"}]}]}


def test_migration_wraps_plain_text_lines():
    assert json.loads(migration.normalized("one\n\ntwo")) == text_doc("one", "", "two")


@pytest.mark.parametrize("raw", ["", None])
def test_migration_turns_empty_content_into_an_empty_doc(raw):
    assert json.loads(migration.normalized(raw)) == {"type": "doc", "content": []}


def test_migration_unwraps_json_string_values():
    assert json.loads(migration.normalized('"quoted"')) == text_doc("quoted")


@pytest.mark.parametrize("raw", ["42", "[1, 2]", "{'a': 1}", "null"])
def test_migration_keeps_other_values_as_literal_text(raw):
    assert json.loads(migration.normalized(raw)) == text_doc(raw)


def test_migration_is_idempotent():
    for raw in ("hello", repr(DOC), json.dumps(DOC), ""):
        once = migration.normalized(raw)
        assert migration.normalized(once) == once


def test_upgrade_rewrites_stored_rows(client):
    alice = register(client, "alice-mig")
    legacy = {
        "plain": "hello\nworld",
        "python": repr(DOC),
        "spaced": json.dumps(DOC),
        "current": serialize(DOC),
        "blank": "",
    }

    async def run():
        ids = {}
        for name, content in legacy.items():
            row = await Message.create(
                uuid=uuid.uuid4(), content=content, author_id=alice["id"],
                timestamp=datetime.now(timezone.utc))
            ids[name] = row.id
        await migration.upgrade(connections.get("default"))
        return {name: (await Message.get(id=pk)).content for name, pk in ids.items()}

    stored = client.portal.call(run)
    assert json.loads(stored["plain"]) == text_doc("hello", "world")
    assert stored["python"] == stored["spaced"] == stored["current"] == serialize(DOC)
    assert json.loads(stored["blank"]) == {"type": "doc", "content": []}
