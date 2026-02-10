from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "users" DROP COLUMN "is_active";"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "users" ADD "is_active" BOOL NOT NULL DEFAULT True;"""


MODELS_STATE = (
    "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"
    "6ku59x67L7jeXxf+4gO00="
)
