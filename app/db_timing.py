import functools
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tortoise.backends.base.client import BaseDBAsyncClient

logger = logging.getLogger("app.db_timing")

SLOW_QUERY_COUNT = 15
SLOW_DB_SECONDS = 0.3

_EXECUTE_METHODS = ("execute_insert", "execute_many", "execute_query", "execute_query_dict", "execute_script")


@dataclass
class QueryStats:
    count: int = 0
    seconds: float = 0.0


_stats: ContextVar[QueryStats | None] = ContextVar("db_query_stats", default=None)
# Backend methods can call each other (a transaction's execute_many calls super()).
_inside_query: ContextVar[bool] = ContextVar("db_inside_query", default=False)


def _timed(method):
    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        stats = _stats.get()
        if stats is None or _inside_query.get():
            return await method(*args, **kwargs)
        token = _inside_query.set(True)
        start = time.perf_counter()
        try:
            return await method(*args, **kwargs)
        finally:
            stats.seconds += time.perf_counter() - start
            stats.count += 1
            _inside_query.reset(token)

    wrapper.__db_timed__ = True
    return wrapper


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def instrument_db_clients() -> None:
    # Importing the backends registers their client classes as subclasses.
    import tortoise.backends.asyncpg.client  # noqa: F401
    import tortoise.backends.sqlite.client  # noqa: F401

    for cls in [BaseDBAsyncClient, *_subclasses(BaseDBAsyncClient)]:
        for name in _EXECUTE_METHODS:
            method = cls.__dict__.get(name)
            if method is not None and not getattr(method, "__db_timed__", False):
                setattr(cls, name, _timed(method))


@contextmanager
def own_query_stats() -> Iterator[QueryStats]:
    """Count the queries of a block apart from the request it started from,
    such as work that runs after the response."""
    stats = QueryStats()
    token = _stats.set(stats)
    try:
        yield stats
    finally:
        _stats.reset(token)


def is_slow(stats: QueryStats) -> bool:
    return stats.count > SLOW_QUERY_COUNT or stats.seconds > SLOW_DB_SECONDS


def _header(app_seconds: float, stats: QueryStats) -> bytes:
    noun = "query" if stats.count == 1 else "queries"
    return (
        f'app;dur={app_seconds * 1000:.1f}, '
        f'db;dur={stats.seconds * 1000:.1f};desc="{stats.count} {noun}"'
    ).encode()


class ServerTimingMiddleware:
    """Adds a Server-Timing header with app time, DB time and query count to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        stats = QueryStats()
        token = _stats.set(stats)
        start = time.perf_counter()

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                elapsed = time.perf_counter() - start
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", _header(elapsed, stats)))
                message = {**message, "headers": headers}
                if is_slow(stats):
                    logger.warning(
                        "Slow request %s %s: %d queries, %.0f ms in the database",
                        scope["method"], scope["path"], stats.count, stats.seconds * 1000,
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _stats.reset(token)
