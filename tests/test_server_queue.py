"""The per-server queue for work that runs after a response."""
import asyncio
import logging

from app.services.server_queue import ServerQueue

LOGGER = "app.server_queue"


def recorder(order, name, pause=0.0):
    async def job():
        await asyncio.sleep(pause)
        order.append(name)

    return job


def test_jobs_of_a_server_run_in_reservation_order():
    async def scenario():
        queue, order = ServerQueue(), []
        first, second = queue.reserve(1), queue.reserve(1)
        second.run(recorder(order, "b"))
        first.run(recorder(order, "a"))
        await queue.drain()
        return order

    assert asyncio.run(scenario()) == ["a", "b"]


def test_a_job_waits_for_a_slow_job_before_it():
    async def scenario():
        queue, order = ServerQueue(), []
        queue.reserve(1).run(recorder(order, "a", pause=0.05))
        queue.reserve(1).run(recorder(order, "b"))
        await queue.drain()
        return order

    assert asyncio.run(scenario()) == ["a", "b"]


def test_servers_do_not_wait_for_each_other():
    async def scenario():
        queue, order = ServerQueue(), []
        unarmed = queue.reserve(1)
        queue.reserve(2).run(recorder(order, "other"))
        await asyncio.sleep(0.05)
        ran_meanwhile = list(order)
        unarmed.cancel()
        await queue.drain()
        return ran_meanwhile

    assert asyncio.run(scenario()) == ["other"]


def test_a_cancelled_slot_lets_the_next_job_run():
    async def scenario():
        queue, order = ServerQueue(), []
        first, second = queue.reserve(1), queue.reserve(1)
        first.cancel()
        second.run(recorder(order, "b"))
        await queue.drain()
        return order

    assert asyncio.run(scenario()) == ["b"]


def test_a_failing_job_is_logged_and_the_next_one_runs(caplog):
    async def broken():
        raise RuntimeError("boom")

    async def scenario():
        queue, order = ServerQueue(), []
        queue.reserve(1).run(broken)
        queue.reserve(1).run(recorder(order, "next"))
        await queue.drain()
        return order

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert asyncio.run(scenario()) == ["next"]
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def test_each_job_logs_its_query_count(caplog):
    async def scenario():
        queue = ServerQueue()
        queue.reserve(1).run(recorder([], "a"))
        await queue.drain()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        asyncio.run(scenario())
    assert [r.queries for r in caplog.records if hasattr(r, "queries")] == [0]


def test_drain_waits_for_reserved_slots():
    async def scenario():
        queue, order = ServerQueue(), []
        slot = queue.reserve(1)
        draining = asyncio.create_task(queue.drain())
        await asyncio.sleep(0.05)
        finished_early = draining.done()
        slot.run(recorder(order, "late"))
        await draining
        return finished_early, order

    assert asyncio.run(scenario()) == (False, ["late"])


def test_shutdown_cancels_jobs_that_outlast_the_wait():
    async def scenario():
        queue, cancelled = ServerQueue(), []

        async def endless():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        queue.reserve(1).run(endless)
        await asyncio.sleep(0.01)
        await queue.shutdown(timeout=0.05)
        return cancelled, queue._tails, queue._tasks

    assert asyncio.run(scenario()) == ([True], {}, set())
