from tortoise import BaseDBAsyncClient

# CREATE INDEX CONCURRENTLY can't run inside a transaction, and Postgres treats
# a multi-statement script as one, so each index is built in its own call.
RUN_IN_TRANSACTION = False

INDEXES = (
    ('"idx_attachments_message_449b8b"', '"attachments" ("message_id")'),
    ('"idx_messages_channel_6d6fe2"', '"messages" ("channel_id", "created_at", "id")'),
    ('"idx_messages_convers_d547c2"', '"messages" ("conversation_id", "created_at", "id")'),
)


async def upgrade(db: BaseDBAsyncClient) -> str:
    concurrently = "" if db.capabilities.dialect == "sqlite" else "CONCURRENTLY "
    for name, target in INDEXES:
        await db.execute_script(f"CREATE INDEX {concurrently}IF NOT EXISTS {name} ON {target};")
    # aerich runs the returned script, and asyncpg crashes on an empty one.
    return "SELECT 1;"


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_attachments_message_449b8b";
        DROP INDEX IF EXISTS "idx_messages_convers_d547c2";
        DROP INDEX IF EXISTS "idx_messages_channel_6d6fe2";"""


MODELS_STATE = (
    "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"
    "Wrkki25Ve1NjeI1jyoGrgqtH+8fPs/FlhH2w=="
)
