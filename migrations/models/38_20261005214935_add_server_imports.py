from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "server_imports" (
    "id" UUID NOT NULL PRIMARY KEY,
    "status" VARCHAR(12) NOT NULL,
    "filename" VARCHAR(255) NOT NULL,
    "size" BIGINT NOT NULL,
    "received" BIGINT NOT NULL DEFAULT 0,
    "source" VARCHAR(200),
    "source_platform" VARCHAR(50),
    "source_name" VARCHAR(200),
    "authors" JSONB NOT NULL,
    "plan" JSONB,
    "result" JSONB,
    "progress" JSONB,
    "failed_step" VARCHAR(12),
    "error" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "server_id" INT NOT NULL REFERENCES "servers" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "server_imports" IS 'An export bundle uploaded to a server, and how its import is going.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "server_imports";"""


MODELS_STATE = (
    "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"
)
