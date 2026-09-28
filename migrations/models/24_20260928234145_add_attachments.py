from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "attachments" (
    "id" UUID NOT NULL PRIMARY KEY,
    "filename" VARCHAR(255) NOT NULL,
    "content_type" VARCHAR(255) NOT NULL,
    "size" INT NOT NULL,
    "kind" VARCHAR(10) NOT NULL,
    "width" INT,
    "height" INT,
    "storage_path" VARCHAR(512) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "channel_id" INT REFERENCES "channels" ("id") ON DELETE CASCADE,
    "conversation_id" INT REFERENCES "conversations" ("id") ON DELETE CASCADE,
    "message_id" INT REFERENCES "messages" ("id") ON DELETE CASCADE,
    "server_id" INT REFERENCES "servers" ("id") ON DELETE CASCADE,
    "uploader_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "attachments" IS 'An uploaded file that belongs to a channel or DM conversation.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "attachments";"""


MODELS_STATE = (
    "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"
)
