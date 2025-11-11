from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "tokens" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "token" VARCHAR(255) NOT NULL UNIQUE,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "tokens";"""


MODELS_STATE = (
    "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"
)
