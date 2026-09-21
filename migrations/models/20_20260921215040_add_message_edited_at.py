from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "messages" ADD "edited_at" TIMESTAMPTZ;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "messages" DROP COLUMN "edited_at";"""


MODELS_STATE = (
    "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"
)
