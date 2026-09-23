from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "conversations" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "user_a_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "user_b_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_conversatio_user_a__053db6" UNIQUE ("user_a_id", "user_b_id")
);
COMMENT ON TABLE "conversations" IS 'A 1:1 direct-message thread between two users.';
        ALTER TABLE "messages" ADD "conversation_id" INT;
        ALTER TABLE "messages" ALTER COLUMN "channel_id" DROP NOT NULL;
        ALTER TABLE "messages" ALTER COLUMN "server_id" DROP NOT NULL;
        ALTER TABLE "messages" ADD CONSTRAINT "fk_messages_conversa_c44a3ed5" FOREIGN KEY ("conversation_id") REFERENCES "conversations" ("id") ON DELETE CASCADE;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "messages" DROP CONSTRAINT IF EXISTS "fk_messages_conversa_c44a3ed5";
        ALTER TABLE "messages" DROP COLUMN "conversation_id";
        ALTER TABLE "messages" ALTER COLUMN "channel_id" SET NOT NULL;
        ALTER TABLE "messages" ALTER COLUMN "server_id" SET NOT NULL;
        DROP TABLE IF EXISTS "conversations";"""


MODELS_STATE = (
    "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"
)
