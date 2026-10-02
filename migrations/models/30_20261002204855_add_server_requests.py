from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "server_requests" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "description" TEXT NOT NULL,
    "expected_size" VARCHAR(8) NOT NULL,
    "status" VARCHAR(10) NOT NULL DEFAULT 'pending',
    "decline_reason" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "decided_at" TIMESTAMPTZ,
    "decided_by_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "server_id" INT REFERENCES "servers" ("id") ON DELETE SET NULL,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_server_requ_status_27ffa7" ON "server_requests" ("status");
COMMENT ON TABLE "server_requests" IS 'A user''s request to create a server while creation is gated.';
        ALTER TABLE "users" ADD "is_platform_admin" BOOL NOT NULL DEFAULT False;
        CREATE UNIQUE INDEX "uniq_server_requests_one_pending" ON "server_requests" ("user_id") WHERE status = 'pending';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uniq_server_requests_one_pending";
        ALTER TABLE "users" DROP COLUMN "is_platform_admin";
        DROP TABLE IF EXISTS "server_requests";"""


MODELS_STATE = (
    "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"
    "p9jTIkGyLU+LtVn8V+ZTUcMYvfrPy7f/A0XvmXU="
)
