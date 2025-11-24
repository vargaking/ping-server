from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" ADD "type" VARCHAR(10) NOT NULL DEFAULT 'text';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" DROP COLUMN "type";"""


MODELS_STATE = (
    "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"
    "coGUc="
)
