from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" DROP CONSTRAINT IF EXISTS "fk_channels_server_dfebb7db";
        ALTER TABLE "channels" RENAME COLUMN "server_id_id" TO "server_id";
        ALTER TABLE "channels" ADD CONSTRAINT "fk_channels_server_65391822" FOREIGN KEY ("server_id") REFERENCES "server" ("id") ON DELETE CASCADE;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "channels" DROP CONSTRAINT IF EXISTS "fk_channels_server_65391822";
        ALTER TABLE "channels" RENAME COLUMN "server_id" TO "server_id_id";
        ALTER TABLE "channels" ADD CONSTRAINT "fk_channels_server_dfebb7db" FOREIGN KEY ("server_id_id") REFERENCES "server" ("id") ON DELETE CASCADE;"""


MODELS_STATE = (
    "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"
    "LE//Ax3jbIQ="
)
