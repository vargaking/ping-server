import time
from datetime import datetime, timezone

from app.models.User import User

WRITE_INTERVAL_SECONDS = 3600

clock = time.monotonic
_last_written: dict[int, float] = {}


async def touch_last_active(user_id: int) -> None:
    """Stamp the user's last_active_at, at most once an hour per process."""
    now = clock()
    last = _last_written.get(user_id)
    if last is not None and now - last < WRITE_INTERVAL_SECONDS:
        return
    _last_written[user_id] = now
    await User.filter(id=user_id).update(last_active_at=datetime.now(timezone.utc))


def forget_all() -> None:
    _last_written.clear()
