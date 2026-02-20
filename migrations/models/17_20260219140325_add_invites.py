from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "invites" (
    "id" UUID NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "valid_until" TIMESTAMPTZ,
    "max_uses" INT,
    "use_count" INT NOT NULL DEFAULT 0,
    "password_hash" TEXT,
    "is_active" BOOL NOT NULL DEFAULT True,
    "created_by_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "server_id" INT NOT NULL REFERENCES "servers" ("id") ON DELETE CASCADE
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "invites";"""


MODELS_STATE = (
    "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"
    "/h+nVGAk"
)
