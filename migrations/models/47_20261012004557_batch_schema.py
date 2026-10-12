from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "announcements" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "text" VARCHAR(500) NOT NULL,
    "level" VARCHAR(16) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "expires_at" TIMESTAMPTZ,
    "created_by_id" INT REFERENCES "users" ("id") ON DELETE SET NULL
);
COMMENT ON TABLE "announcements" IS 'A platform-wide notice posted by a platform admin.';
        CREATE TABLE IF NOT EXISTS "announcement_dismissals" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "announcement_id" INT NOT NULL REFERENCES "announcements" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_announcemen_announc_f2caa4" UNIQUE ("announcement_id", "user_id")
);
CREATE INDEX IF NOT EXISTS "idx_announcemen_user_id_9979db" ON "announcement_dismissals" ("user_id");
COMMENT ON TABLE "announcement_dismissals" IS 'A user having dismissed an announcement.';
        CREATE TABLE IF NOT EXISTS "calls" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "state" VARCHAR(10) NOT NULL,
    "reason" VARCHAR(12),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "ring_expires_at" TIMESTAMPTZ NOT NULL,
    "answered_at" TIMESTAMPTZ,
    "ended_at" TIMESTAMPTZ,
    "open_key" INT UNIQUE,
    "callee_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "caller_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "conversation_id" INT NOT NULL REFERENCES "conversations" ("id") ON DELETE CASCADE,
    "message_id" INT NOT NULL UNIQUE REFERENCES "messages" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_calls_convers_8e1c3a" ON "calls" ("conversation_id");
CREATE INDEX IF NOT EXISTS "idx_calls_caller__dddf4d" ON "calls" ("caller_id");
CREATE INDEX IF NOT EXISTS "idx_calls_callee__9dd9c7" ON "calls" ("callee_id");
COMMENT ON TABLE "calls" IS 'A call in a DM conversation, shown in the thread as its message.';
        CREATE TABLE IF NOT EXISTS "message_mentions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE,
    "message_id" INT NOT NULL REFERENCES "messages" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_message_men_message_33ee85" UNIQUE ("message_id", "user_id")
);
CREATE INDEX IF NOT EXISTS "idx_message_men_user_id_a00695" ON "message_mentions" ("user_id", "channel_id", "message_id");
COMMENT ON TABLE "message_mentions" IS 'A user mentioned by a channel message. DMs get no rows.';
        CREATE TABLE IF NOT EXISTS "server_import_author_changes" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "import_id" UUID,
    "author_id" VARCHAR(255) NOT NULL,
    "author_name" VARCHAR(200) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "actor_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "from_user_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "server_id" INT NOT NULL REFERENCES "servers" ("id") ON DELETE CASCADE,
    "to_user_id" INT REFERENCES "users" ("id") ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS "idx_server_impo_server__26d283" ON "server_import_author_changes" ("server_id", "created_at");
COMMENT ON TABLE "server_import_author_changes" IS 'One change to an import''s author mapping, kept after the import row is gone.';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "server_import_author_changes";
        DROP TABLE IF EXISTS "announcement_dismissals";
        DROP TABLE IF EXISTS "message_mentions";
        DROP TABLE IF EXISTS "calls";
        DROP TABLE IF EXISTS "announcements";"""


MODELS_STATE = (
    "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"
    "o5NDYa3lPD3bv94+Wv/wdSpMVd"
)
