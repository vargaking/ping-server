from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" ADD "topic" VARCHAR(1024);
        -- The frontend used to read an optional topic out of channel_settings.
        UPDATE "channels" SET "topic" = LEFT("channel_settings"->>'topic', 1024)
        WHERE COALESCE("channel_settings"->>'topic', '') <> '';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" DROP COLUMN "topic";"""


MODELS_STATE = (
    "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"
)
