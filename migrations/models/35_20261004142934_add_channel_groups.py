import json

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True

BATCH_SIZE = 200

UPGRADE_DDL = """
        CREATE TABLE IF NOT EXISTS "channel_groups" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "position" INT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "server_id" INT NOT NULL REFERENCES "servers" ("id") ON DELETE CASCADE
);
        ALTER TABLE "channels" ADD "group_id" INT;
        ALTER TABLE "channels" ADD "position" INT NOT NULL DEFAULT 0;
        ALTER TABLE "channels" ADD CONSTRAINT "fk_channels_channel__7c027eb2" FOREIGN KEY ("group_id") REFERENCES "channel_groups" ("id") ON DELETE SET NULL;"""

DOWNGRADE_DDL = """
        ALTER TABLE "channels" DROP CONSTRAINT IF EXISTS "fk_channels_channel__7c027eb2";
        ALTER TABLE "channels" DROP COLUMN "group_id";
        ALTER TABLE "channels" DROP COLUMN "position";
        DROP TABLE IF EXISTS "channel_groups";"""


def _placeholders(db: BaseDBAsyncClient, count: int, start: int = 1) -> str:
    if db.capabilities.dialect == "sqlite":
        return ", ".join("?" * count)
    return ", ".join(f"${n}" for n in range(start, start + count))


def _param(db: BaseDBAsyncClient, n: int) -> str:
    return "?" if db.capabilities.dialect == "sqlite" else f"${n}"


def _settings(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _effective_order(settings: dict, channel_ids: list[int]) -> list[int]:
    """The order the deployed client shows: channel_order first (ints that are
    this server's channels, once each), then everything else by id."""
    known = set(channel_ids)
    order: list[int] = []
    seen: set[int] = set()
    stored = settings.get("channel_order")
    for value in stored if isinstance(stored, list) else []:
        if isinstance(value, int) and not isinstance(value, bool) \
                and value in known and value not in seen:
            order.append(value)
            seen.add(value)
    return order + [cid for cid in sorted(channel_ids) if cid not in seen]


async def _server_batches(db: BaseDBAsyncClient):
    last_id = 0
    select = (
        "SELECT id, server_settings FROM servers "
        f"WHERE id > {_param(db, 1)} ORDER BY id LIMIT {BATCH_SIZE}")
    while True:
        rows = await db.execute_query_dict(select, [last_id])
        if not rows:
            return
        yield rows
        last_id = rows[-1]["id"]


async def _channels_by_server(db: BaseDBAsyncClient, server_ids: list[int], columns: str) -> dict:
    rows = await db.execute_query_dict(
        f"SELECT id, server_id, {columns} FROM channels "
        f"WHERE server_id IN ({_placeholders(db, len(server_ids))}) ORDER BY id",
        server_ids)
    by_server: dict[int, list[dict]] = {sid: [] for sid in server_ids}
    for row in rows:
        by_server[row["server_id"]].append(row)
    return by_server


async def move_channels_into_groups(db: BaseDBAsyncClient) -> None:
    """Give every server a "Text channels" and a "Voice channels" group holding
    its channels in their current order, and drop channel_order."""
    insert_group = (
        'INSERT INTO channel_groups (name, position, created_at, server_id) '
        f"VALUES ({_param(db, 1)}, {_param(db, 2)}, CURRENT_TIMESTAMP, {_param(db, 3)})")
    set_channel = (
        f"UPDATE channels SET group_id = {_param(db, 1)}, position = {_param(db, 2)} "
        f"WHERE id = {_param(db, 3)}")
    set_settings = (
        f"UPDATE servers SET server_settings = {_param(db, 1)} WHERE id = {_param(db, 2)}")

    async for servers in _server_batches(db):
        server_ids = [row["id"] for row in servers]
        await db.execute_many(insert_group, [
            [name, position, sid]
            for sid in server_ids
            for position, name in enumerate(("Text channels", "Voice channels"))
        ])
        group_rows = await db.execute_query_dict(
            "SELECT id, server_id FROM channel_groups "
            f"WHERE server_id IN ({_placeholders(db, len(server_ids))}) "
            "ORDER BY position, id", server_ids)
        groups: dict[int, list[int]] = {sid: [] for sid in server_ids}
        for row in group_rows:
            groups[row["server_id"]].append(row["id"])

        channels = await _channels_by_server(db, server_ids, "type")
        channel_updates = []
        settings_updates = []
        for server in servers:
            settings = _settings(server["server_settings"])
            rows = {row["id"]: row for row in channels[server["id"]]}
            text_group, voice_group = groups[server["id"]]
            next_position = {text_group: 0, voice_group: 0}
            for channel_id in _effective_order(settings, list(rows)):
                group_id = voice_group if rows[channel_id]["type"] == "voice" else text_group
                channel_updates.append([group_id, next_position[group_id], channel_id])
                next_position[group_id] += 1
            if "channel_order" in settings:
                del settings["channel_order"]
                settings_updates.append([json.dumps(settings), server["id"]])
        if channel_updates:
            await db.execute_many(set_channel, channel_updates)
        if settings_updates:
            await db.execute_many(set_settings, settings_updates)


async def restore_channel_order(db: BaseDBAsyncClient) -> None:
    """Write the flattened layout (ungrouped, then each group) back to
    server_settings.channel_order."""
    set_settings = (
        f"UPDATE servers SET server_settings = {_param(db, 1)} WHERE id = {_param(db, 2)}")

    async for servers in _server_batches(db):
        server_ids = [row["id"] for row in servers]
        group_rows = await db.execute_query_dict(
            "SELECT id, server_id, position FROM channel_groups "
            f"WHERE server_id IN ({_placeholders(db, len(server_ids))})", server_ids)
        group_rank = {row["id"]: (row["position"], row["id"]) for row in group_rows}
        channels = await _channels_by_server(db, server_ids, "group_id, position")

        updates = []
        for server in servers:
            def sort_key(row):
                group_id = row["group_id"]
                return (
                    group_id is not None,
                    group_rank.get(group_id, (0, 0)),
                    row["position"],
                    row["id"],
                )
            ordered = sorted(channels[server["id"]], key=sort_key)
            settings = _settings(server["server_settings"])
            settings["channel_order"] = [row["id"] for row in ordered]
            updates.append([json.dumps(settings), server["id"]])
        await db.execute_many(set_settings, updates)


async def upgrade(db: BaseDBAsyncClient) -> str:
    # aerich runs the returned SQL only after this function, so the schema has
    # to exist before the data step.
    await db.execute_script(UPGRADE_DDL)
    await move_channels_into_groups(db)
    return "SELECT 1;"


async def downgrade(db: BaseDBAsyncClient) -> str:
    await restore_channel_order(db)
    return DOWNGRADE_DDL


MODELS_STATE = (
    "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"
)
