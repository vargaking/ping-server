from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "messagetochannel";"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "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"
    "/C7pK4ijrUl3P+Mka/MLy9P/osSnqQ=="
)
