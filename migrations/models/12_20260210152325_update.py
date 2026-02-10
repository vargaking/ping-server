from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" ADD "server_id_id" INT NOT NULL;
        ALTER TABLE "channels" ALTER COLUMN "channel_settings" TYPE JSONB USING "channel_settings"::JSONB;
        ALTER TABLE "messages" ALTER COLUMN "metadata" TYPE JSONB USING "metadata"::JSONB;
        ALTER TABLE "roles" ALTER COLUMN "settings" TYPE JSONB USING "settings"::JSONB;
        ALTER TABLE "server" ALTER COLUMN "server_profile" TYPE JSONB USING "server_profile"::JSONB;
        ALTER TABLE "server" ALTER COLUMN "server_settings" TYPE JSONB USING "server_settings"::JSONB;
        ALTER TABLE "users" ALTER COLUMN "profile" TYPE JSONB USING "profile"::JSONB;
        DROP TABLE IF EXISTS "channeltoserver";
        ALTER TABLE "channels" ADD CONSTRAINT "fk_channels_server_dfebb7db" FOREIGN KEY ("server_id_id") REFERENCES "server" ("id") ON DELETE CASCADE;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" DROP CONSTRAINT IF EXISTS "fk_channels_server_dfebb7db";
        ALTER TABLE "roles" ALTER COLUMN "settings" TYPE JSONB USING "settings"::JSONB;
        ALTER TABLE "users" ALTER COLUMN "profile" TYPE JSONB USING "profile"::JSONB;
        ALTER TABLE "server" ALTER COLUMN "server_profile" TYPE JSONB USING "server_profile"::JSONB;
        ALTER TABLE "server" ALTER COLUMN "server_settings" TYPE JSONB USING "server_settings"::JSONB;
        ALTER TABLE "channels" DROP COLUMN "server_id_id";
        ALTER TABLE "channels" ALTER COLUMN "channel_settings" TYPE JSONB USING "channel_settings"::JSONB;
        ALTER TABLE "messages" ALTER COLUMN "metadata" TYPE JSONB USING "metadata"::JSONB;"""


MODELS_STATE = (
    "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"
    "/91T6YfwX0AcbUy6+xWZrPVvLE//A8wecTQ="
)
