from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "stat_samples" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "scope" VARCHAR(32) NOT NULL DEFAULT 'platform',
    "metric" VARCHAR(32) NOT NULL,
    "resolution" VARCHAR(8) NOT NULL,
    "bucket" TIMESTAMPTZ NOT NULL,
    "avg" DOUBLE PRECISION NOT NULL,
    "max" DOUBLE PRECISION NOT NULL,
    CONSTRAINT "uid_stat_sample_scope_f29e45" UNIQUE ("scope", "metric", "resolution", "bucket")
);
CREATE INDEX IF NOT EXISTS "idx_stat_sample_scope_50cf89" ON "stat_samples" ("scope", "resolution", "bucket");
COMMENT ON TABLE "stat_samples" IS 'One aggregate reading of a metric for a minute or an hour.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "stat_samples";"""


MODELS_STATE = (
"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"
    "79ncZvq2L/t/8Ddu8Fcg=="
)
