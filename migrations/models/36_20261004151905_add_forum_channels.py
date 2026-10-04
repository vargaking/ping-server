from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "forum_posts" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL,
    "pinned" BOOL NOT NULL DEFAULT False,
    "locked" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_activity_at" TIMESTAMPTZ NOT NULL,
    "reply_count" INT NOT NULL DEFAULT 0,
    "metadata" JSONB NOT NULL,
    "opening_message_id" INT UNIQUE,
    "author_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_forum_posts_channel_f4db33" ON "forum_posts" ("channel_id", "last_activity_at");
        CREATE TABLE IF NOT EXISTS "forum_tags" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(30) NOT NULL,
    "color" VARCHAR(7),
    "position" INT NOT NULL DEFAULT 0,
    "channel_id" INT NOT NULL REFERENCES "channels" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_forum_tags_channel_7d1632" UNIQUE ("channel_id", "name")
);
        CREATE TABLE IF NOT EXISTS "forum_post_tags" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "post_id" INT NOT NULL REFERENCES "forum_posts" ("id") ON DELETE CASCADE,
    "tag_id" INT NOT NULL REFERENCES "forum_tags" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_forum_post__post_id_b84ab8" UNIQUE ("post_id", "tag_id")
);
        ALTER TABLE "messages" ADD "post_id" INT;
        ALTER TABLE "messages" ADD CONSTRAINT "fk_messages_forum_po_a61176cc" FOREIGN KEY ("post_id") REFERENCES "forum_posts" ("id") ON DELETE CASCADE;
        CREATE INDEX IF NOT EXISTS "idx_messages_post_id_f9db4e" ON "messages" ("post_id", "timestamp");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_messages_post_id_f9db4e";
        ALTER TABLE "messages" DROP CONSTRAINT IF EXISTS "fk_messages_forum_po_a61176cc";
        ALTER TABLE "messages" DROP COLUMN "post_id";
        DROP TABLE IF EXISTS "forum_post_tags";
        DROP TABLE IF EXISTS "forum_tags";
        DROP TABLE IF EXISTS "forum_posts";"""


MODELS_STATE = (
    "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"
)
