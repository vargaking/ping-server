import ast
import json

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True

BATCH_SIZE = 500
MAX_LITERAL_EVAL_CHARS = 200_000


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_doc(value) -> bool:
    return isinstance(value, dict) and value.get("type") == "doc"


def _plain_text_doc(text: str) -> str:
    if not text:
        return _dump({"type": "doc", "content": []})
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line
        else {"type": "paragraph"}
        for line in text.split("\n")
    ]
    return _dump({"type": "doc", "content": paragraphs})


def normalized(raw: str | None) -> str:
    """The stored form of a message's content: a compact JSON TipTap doc."""
    text = raw or ""
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
        if len(text) <= MAX_LITERAL_EVAL_CHARS:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                pass
    if _is_doc(parsed):
        return _dump(parsed)
    if isinstance(parsed, str):
        return _plain_text_doc(parsed)
    return _plain_text_doc(text)


async def upgrade(db: BaseDBAsyncClient) -> str:
    first, second = ("?", "?") if db.capabilities.dialect == "sqlite" else ("$1", "$2")
    select = f"SELECT id, content FROM messages WHERE id > {first} ORDER BY id LIMIT {BATCH_SIZE}"
    update = f"UPDATE messages SET content = {first} WHERE id = {second}"
    last_id = 0
    while True:
        rows = await db.execute_query_dict(select, [last_id])
        if not rows:
            break
        for row in rows:
            content = normalized(row["content"])
            if content != row["content"]:
                await db.execute_query(update, [content, row["id"]])
        last_id = rows[-1]["id"]
    # aerich runs the returned script, and asyncpg crashes on an empty one.
    return "SELECT 1;"


async def downgrade(db: BaseDBAsyncClient) -> str:
    return "SELECT 1;"


MODELS_STATE = (
    "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"
    "99NS5c"
)
