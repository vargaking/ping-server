"""The message index migration, run against the test database."""
import importlib.util
from pathlib import Path

import pytest
from tortoise import connections

from tests.test_permissions import run

MIGRATION = next(Path(__file__).parent.parent.glob("migrations/models/*_message_indexes.py"))
NAMES = {"idx_attachments_message_449b8b", "idx_messages_channel_6d6fe2", "idx_messages_convers_d547c2"}


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("message_indexes", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _indexes() -> set[str]:
    rows = await connections.get("default").execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type = 'index'")
    return {row["name"] for row in rows} & NAMES


def test_models_declare_the_indexes(client):
    assert run(client, _indexes) == NAMES


def test_downgrade_drops_and_upgrade_rebuilds(client, migration):
    async def roundtrip():
        db = connections.get("default")
        await db.execute_script(await migration.downgrade(db))
        dropped = await _indexes()
        await db.execute_script(await migration.upgrade(db))
        return dropped, await _indexes()

    dropped, rebuilt = run(client, roundtrip)
    assert dropped == set()
    assert rebuilt == NAMES
    assert migration.RUN_IN_TRANSACTION is False
