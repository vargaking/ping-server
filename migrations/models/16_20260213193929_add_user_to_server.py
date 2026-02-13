from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "server" RENAME TO "servers";
        ALTER TABLE "usertoserver" RENAME TO "user_to_server";
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_user_to_ser_user_id_8376d2" ON "user_to_server" ("user_id", "server_id");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_user_to_ser_user_id_8376d2";
        ALTER TABLE "servers" RENAME TO "server";
        ALTER TABLE "user_to_server" RENAME TO "usertoserver";"""


MODELS_STATE = (
    "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"
)
