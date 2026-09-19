from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "servers" ADD "owner_id" INT REFERENCES "users" ("id") ON DELETE SET NULL;
        UPDATE "servers" SET "owner_id" = (
            SELECT "user_id" FROM "user_to_server"
            WHERE "user_to_server"."server_id" = "servers"."id"
            ORDER BY "created_at" ASC, "id" ASC
            LIMIT 1
        );"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "servers" DROP COLUMN "owner_id";"""


MODELS_STATE = (
    "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"
    "DzVr+wPP8PvSyECQ=="
)
