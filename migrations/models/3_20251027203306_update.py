from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "users" ADD "password_hash" TEXT NOT NULL;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "users" DROP COLUMN "password_hash";"""


MODELS_STATE = (
    "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"
)
