from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "reactions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "emoji" VARCHAR(64) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "message_id" INT NOT NULL REFERENCES "messages" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_reactions_message_ff253e" UNIQUE ("message_id", "user_id", "emoji")
);
CREATE INDEX IF NOT EXISTS "idx_reactions_message_906d94" ON "reactions" ("message_id");
COMMENT ON TABLE "reactions" IS 'One user''s emoji reaction to a message.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "reactions";"""


MODELS_STATE = (
    "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"
    "V/Xh7/DxwJLvk="
)
