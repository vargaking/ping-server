from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


TABLE = """
        CREATE TABLE IF NOT EXISTS "permission_overwrites" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "allow" BIGINT NOT NULL DEFAULT 0,
    "deny" BIGINT NOT NULL DEFAULT 0,
    "channel_id" INT REFERENCES "channels" ("id") ON DELETE CASCADE,
    "group_id" INT REFERENCES "channel_groups" ("id") ON DELETE CASCADE,
    "role_id" INT REFERENCES "roles" ("id") ON DELETE CASCADE,
    "server_id" INT NOT NULL REFERENCES "servers" ("id") ON DELETE CASCADE,
    "user_id" INT REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "permission_overwrites" IS 'What a role or a member may do in one channel or category, on top of the';
CREATE INDEX IF NOT EXISTS "idx_permission_overwrites_server" ON "permission_overwrites" ("server_id");"""

# One row per (target, subject). A plain unique key would treat the NULL
# columns as all different, so each pair gets a partial index.
UNIQUE_PAIRS = (
    ("channel", "role"), ("channel", "user"), ("group", "role"), ("group", "user"),
)

CHECKS = """
ALTER TABLE "permission_overwrites" ADD CONSTRAINT "ck_overwrite_one_target"
    CHECK (("channel_id" IS NULL) <> ("group_id" IS NULL));
ALTER TABLE "permission_overwrites" ADD CONSTRAINT "ck_overwrite_one_subject"
    CHECK (("role_id" IS NULL) <> ("user_id" IS NULL));
ALTER TABLE "permission_overwrites" ADD CONSTRAINT "ck_overwrite_disjoint"
    CHECK (("allow" & "deny") = 0);"""


async def upgrade(db: BaseDBAsyncClient) -> str:
    script = TABLE
    for target, subject in UNIQUE_PAIRS:
        script += (
            f'\nCREATE UNIQUE INDEX IF NOT EXISTS "uid_overwrite_{target}_{subject}" '
            f'ON "permission_overwrites" ("{target}_id", "{subject}_id") '
            f'WHERE "{target}_id" IS NOT NULL AND "{subject}_id" IS NOT NULL;')
    if db.capabilities.dialect != "sqlite":
        script += CHECKS
    return script


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "permission_overwrites";"""


MODELS_STATE = (
    "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"
    "OJZFN+ZytzE2nMw6yGK0fzx8vX/wOsAyB6"
)
