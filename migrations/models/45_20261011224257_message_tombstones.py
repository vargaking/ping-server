from tortoise import BaseDBAsyncClient

# CREATE INDEX CONCURRENTLY can't run inside a transaction, so the messages index
# is built in its own call after the table, as in migration 39.
RUN_IN_TRANSACTION = False

TABLE = """
        CREATE TABLE IF NOT EXISTS "message_tombstones" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "message_uuid" UUID NOT NULL,
    "server_id" INT,
    "deleted_at" TIMESTAMPTZ NOT NULL,
    "channel_id" INT REFERENCES "channels" ("id") ON DELETE CASCADE,
    "conversation_id" INT REFERENCES "conversations" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_message_tom_deleted_442227" ON "message_tombstones" ("deleted_at");
COMMENT ON TABLE "message_tombstones" IS 'A deleted message''s id, kept for the changes feed until it is pruned.';"""


async def upgrade(db: BaseDBAsyncClient) -> str:
    await db.execute_script(TABLE)
    concurrently = "" if db.capabilities.dialect == "sqlite" else "CONCURRENTLY "
    await db.execute_script(
        f'CREATE INDEX {concurrently}IF NOT EXISTS "idx_messages_edited__34c416" ON "messages" ("edited_at");')
    # aerich runs the returned script, and asyncpg crashes on an empty one.
    return "SELECT 1;"


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_messages_edited__34c416";
        DROP TABLE IF EXISTS "message_tombstones";"""


MODELS_STATE = (
    "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"
)
