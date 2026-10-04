from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True

UPGRADE_DDL = """
        ALTER TABLE "roles" ADD "position" INT NOT NULL DEFAULT 0;
        ALTER TABLE "roles" ADD "color" VARCHAR(7);"""

DOWNGRADE_DDL = """
        ALTER TABLE "roles" DROP COLUMN "color";
        ALTER TABLE "roles" DROP COLUMN "position";"""


def _param(db: BaseDBAsyncClient, n: int) -> str:
    return "?" if db.capabilities.dialect == "sqlite" else f"${n}"


async def number_roles(db: BaseDBAsyncClient) -> None:
    """Rank every server's roles: @everyone 0, the others 1..n in id order."""
    rows = await db.execute_query_dict(
        "SELECT id, server_id, is_default FROM roles ORDER BY server_id, id")
    next_position: dict[int, int] = {}
    updates = []
    for row in rows:
        if row["is_default"]:
            updates.append([0, row["id"]])
            continue
        position = next_position.get(row["server_id"], 1)
        next_position[row["server_id"]] = position + 1
        updates.append([position, row["id"]])
    if updates:
        await db.execute_many(
            f"UPDATE roles SET position = {_param(db, 1)} WHERE id = {_param(db, 2)}", updates)


async def upgrade(db: BaseDBAsyncClient) -> str:
    # aerich runs the returned SQL only after this function, so the columns
    # have to exist before the data step.
    await db.execute_script(UPGRADE_DDL)
    await number_roles(db)
    return "SELECT 1;"


async def downgrade(db: BaseDBAsyncClient) -> str:
    return DOWNGRADE_DDL


MODELS_STATE = (
"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"
)
