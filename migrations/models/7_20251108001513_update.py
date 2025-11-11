from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "messages" ADD "uuid" UUID NOT NULL UNIQUE;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_messages_uuid_7574dd";
        ALTER TABLE "messages" DROP COLUMN "uuid";"""


MODELS_STATE = (
    "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"
    "99MXmwMvHqNYo9yl9eHv8H/O3PpA=="
)
