from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "read_states" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "last_read_message_id" INT NOT NULL,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "channel_id" INT REFERENCES "channels" ("id") ON DELETE CASCADE,
    "conversation_id" INT REFERENCES "conversations" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_read_states_user_id_f7f441" UNIQUE ("user_id", "channel_id"),
    CONSTRAINT "uid_read_states_user_id_4b6c79" UNIQUE ("user_id", "conversation_id")
);
COMMENT ON TABLE "read_states" IS 'How far a user has read into a channel or a DM conversation.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "read_states";"""


MODELS_STATE = (
    "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"
    "W/WF7/DxUiB/k="
)
