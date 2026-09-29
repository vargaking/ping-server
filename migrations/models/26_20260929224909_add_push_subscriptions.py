from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "push_subscriptions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "endpoint" VARCHAR(2048) NOT NULL UNIQUE,
    "p256dh" VARCHAR(128) NOT NULL,
    "auth" VARCHAR(64) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_used_at" TIMESTAMPTZ,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "push_subscriptions" IS 'A browser''s Web Push subscription; a user can have several.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "push_subscriptions";"""


MODELS_STATE = (
    "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"
    "ms+ygkdt+Z+Xx/8DUhv30Q=="
)
