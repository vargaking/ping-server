import asyncio
import logging
from collections.abc import Callable, Iterable

from fastapi import WebSocket

logger = logging.getLogger("app.socket_send")

# Read at call time so tests can shorten them.
SEND_TIMEOUT_SECONDS = 5.0
CLOSE_TIMEOUT_SECONDS = 5.0

OnStalled = Callable[[WebSocket], None]


async def send_frame(
    websocket: WebSocket, frame: dict, on_stalled: OnStalled | None = None
) -> bool:
    """Send *frame*, giving up after SEND_TIMEOUT_SECONDS. Returns whether it was sent.

    A send that times out hands the socket to *on_stalled*. Any other failure
    is logged and skipped: the receive loop notices a closed socket on its own.
    """
    try:
        await asyncio.wait_for(websocket.send_json(frame), SEND_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Sending %s timed out; dropping the socket", frame.get("type"))
        if on_stalled is not None:
            on_stalled(websocket)
        return False
    except Exception:
        logger.warning("Failed to send %s", frame.get("type"), exc_info=True)
        return False
    return True


async def send_frame_to_all(
    websockets: Iterable[WebSocket], frame: dict, on_stalled: OnStalled | None = None
) -> None:
    """Send *frame* to every socket at once and return when each send has
    finished or timed out. One slow or dead socket never holds up the others,
    and the caller's next frame starts only after this one is out."""
    await asyncio.gather(*(send_frame(ws, frame, on_stalled) for ws in websockets))


async def accept_within_timeout(websocket: WebSocket) -> bool:
    """Accept the handshake, or close the socket and return False if it takes
    longer than SEND_TIMEOUT_SECONDS."""
    try:
        await asyncio.wait_for(websocket.accept(), SEND_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("WebSocket accept timed out")
        await close_quietly(websocket)
        return False
    return True


async def close_quietly(websocket: WebSocket, code: int = 1000) -> None:
    """Close within CLOSE_TIMEOUT_SECONDS. A socket that is already gone or
    won't finish closing is fine."""
    try:
        await asyncio.wait_for(websocket.close(code=code), CLOSE_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Socket close failed or timed out", exc_info=True)
