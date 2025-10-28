from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "channels" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "channel_settings" JSONB NOT NULL
);
        CREATE TABLE IF NOT EXISTS "channeltoserver" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE,
    "server_id" INT NOT NULL REFERENCES "server" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "messages" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "content" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "metadata" JSONB NOT NULL,
    "author_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "messagetochannel" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE,
    "message_id" INT NOT NULL REFERENCES "messages" ("id") ON DELETE CASCADE
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "channels";
        DROP TABLE IF EXISTS "messagetochannel";
        DROP TABLE IF EXISTS "messages";
        DROP TABLE IF EXISTS "channeltoserver";"""


MODELS_STATE = (
    "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"
    "7VliB8ieRV9cfL0/+G00fX"
)
