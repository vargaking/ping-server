import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..db_timing import is_slow, own_query_stats

logger = logging.getLogger("app.server_queue")

SHUTDOWN_WAIT = 5.0

Job = Callable[[], Awaitable[None]]


class Slot:
    """A job's place in its server's queue, taken before the job is known."""

    def __init__(
        self, queue: "ServerQueue", server_id: int,
        previous: asyncio.Future | None, done: asyncio.Future,
    ) -> None:
        self._queue = queue
        self.server_id = server_id
        self.previous = previous
        self.done = done

    def run(self, job: Job) -> None:
        """Start *job* once the jobs before it are done. Call it outside any
        transaction: the task copies the caller's context, and Tortoise keeps the
        open transaction there."""
        self._queue._start(self, job)

    def cancel(self) -> None:
        """Give the place up without a job."""
        self._queue._start(self, None)


class ServerQueue:
    """Background jobs run one at a time per server, in the order their slots
    were reserved. One process owns every socket, so in-process order is enough."""

    def __init__(self) -> None:
        self._tails: dict[int, asyncio.Future[None]] = {}
        self._tasks: set[asyncio.Task] = set()

    def reserve(self, server_id: int) -> Slot:
        done = asyncio.get_running_loop().create_future()
        previous = self._tails.get(server_id)
        self._tails[server_id] = done
        done.add_done_callback(lambda future: self._forget(server_id, future))
        return Slot(self, server_id, previous, done)

    def _forget(self, server_id: int, done: asyncio.Future) -> None:
        if self._tails.get(server_id) is done:
            del self._tails[server_id]

    def _start(self, slot: Slot, job: Job | None) -> None:
        task = asyncio.create_task(self._run(slot, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, slot: Slot, job: Job | None) -> None:
        try:
            if slot.previous is not None:
                await asyncio.wait([slot.previous])
            if job is not None:
                await self._timed(slot.server_id, job)
        finally:
            if not slot.done.done():
                slot.done.set_result(None)

    @staticmethod
    async def _timed(server_id: int, job: Job) -> None:
        with own_query_stats() as stats:
            start = time.perf_counter()
            try:
                await job()
            except Exception:
                logger.warning("Background job for server %s failed", server_id, exc_info=True)
            logger.log(
                logging.WARNING if is_slow(stats) else logging.DEBUG,
                "Background job for server %s: %d queries, %.0f ms in the database, %.0f ms in all",
                server_id, stats.count, stats.seconds * 1000,
                (time.perf_counter() - start) * 1000,
                extra={"queries": stats.count})

    async def drain(self) -> None:
        """Wait until every reserved slot has been used and its job is done."""
        while self._tails:
            await asyncio.wait(list(self._tails.values()))
            await asyncio.sleep(0)

    async def shutdown(self, timeout: float = SHUTDOWN_WAIT) -> None:
        """Let the jobs finish for *timeout* seconds, then cancel what is left."""
        tails = list(self._tails.values())
        if tails:
            await asyncio.wait(tails, timeout=timeout)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for done in tails:
            if not done.done():
                done.set_result(None)
        # The futures belong to this event loop.
        self._tails.clear()
        self._tasks.clear()


server_queue = ServerQueue()
