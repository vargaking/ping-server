from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_messages_uuid_7574dd";
        ALTER TABLE "messages" DROP COLUMN "uuid";"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "messages" ADD "uuid" UUID NOT NULL UNIQUE;"""


MODELS_STATE = (
    "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"
)
