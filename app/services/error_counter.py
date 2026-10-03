import time
from collections import deque
from datetime import datetime, timezone

WINDOW_SECONDS = 24 * 3600


class ErrorCounter:
    """Counts events in a rolling 24 hour window. Per-process, so it starts
    empty after a restart."""

    clock = staticmethod(time.monotonic)

    def __init__(self) -> None:
        self._events: deque[float] = deque()

    def record(self) -> None:
        self._events.append(self.clock())

    def count(self) -> int:
        cutoff = self.clock() - WINDOW_SECONDS
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events)


client_errors = ErrorCounter()
server_5xx = ErrorCounter()
started_at = datetime.now(timezone.utc)
