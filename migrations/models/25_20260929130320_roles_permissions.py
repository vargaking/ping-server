from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "roles" DROP CONSTRAINT IF EXISTS "roles_name_key";
        DROP INDEX IF EXISTS "uid_roles_name_3e8175";
        ALTER TABLE "roles" ADD "deny" BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE "roles" ADD "allow" BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE "roles" ADD "parent_id" INT;
        ALTER TABLE "roles" ADD "is_default" BOOL NOT NULL DEFAULT False;
        ALTER TABLE "roles" ADD CONSTRAINT "fk_roles_roles_8b9bccfb" FOREIGN KEY ("parent_id") REFERENCES "roles" ("id") ON DELETE SET NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_roles_one_default" ON "roles" ("server_id") WHERE "is_default";
        INSERT INTO "roles" ("name", "server_id", "allow", "deny", "is_default", "settings", "created_at")
        SELECT '@everyone', s."id", 123, 0, TRUE, '{}', CURRENT_TIMESTAMP FROM "servers" s
        WHERE NOT EXISTS (SELECT 1 FROM "roles" r WHERE r."server_id" = s."id" AND r."is_default");
        INSERT INTO "roles" ("name", "server_id", "allow", "deny", "is_default", "settings", "created_at")
        SELECT 'Admin', s."id", 1023, 0, FALSE, '{}', CURRENT_TIMESTAMP FROM "servers" s
        ON CONFLICT ("name", "server_id") DO NOTHING;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    # The global UNIQUE on roles.name is not restored: two servers can now
    # both have an "Admin" role, which that constraint would reject.
    return """
        DELETE FROM "roles" WHERE "is_default" OR ("name" = 'Admin' AND "allow" = 1023);
        DROP INDEX IF EXISTS "uid_roles_one_default";
        ALTER TABLE "roles" DROP CONSTRAINT IF EXISTS "fk_roles_roles_8b9bccfb";
        ALTER TABLE "roles" DROP COLUMN "deny";
        ALTER TABLE "roles" DROP COLUMN "allow";
        ALTER TABLE "roles" DROP COLUMN "parent_id";
        ALTER TABLE "roles" DROP COLUMN "is_default";"""


MODELS_STATE = (
    "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"
    "BoUfEl+bu25lbqOlOeYquI2W/3p5/D/YUnHH"
)
