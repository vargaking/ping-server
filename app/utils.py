import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager

from .models.Token import Token
from .services.stats_history import StatsSampler, sampling_enabled
from .services.voice_moderation import VoiceModeration
from .services.voice_presence import create_voice_presence

logger = logging.getLogger("app.utils")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan. Voice now runs on LiveKit, so there is no media
    proxy client to manage here anymore.

    On startup we prune expired session tokens and stale attachments. Tortoise
    is already initialised at this point (register_tortoise wraps this
    lifespan), but keep it guarded so a housekeeping hiccup can never block the
    app from starting.

    It also runs the voice presence poller when LiveKit is configured, and the
    stats sampler when STATS_SAMPLING is set.
    """
    try:
        deleted = await Token.filter(
            expires_at__not_isnull=True,
            expires_at__lt=datetime.now(timezone.utc),
        ).delete()
        if deleted:
            logger.info("Pruned %s expired session token(s) on startup", deleted)
    except Exception:
        logger.warning("Failed to prune expired tokens on startup", exc_info=True)

    try:
        # Imported here because the push service imports models that import this module.
        from .services.push import push
        push.configure()
    except Exception:
        logger.warning("Failed to configure push", exc_info=True)

    presence = None
    presence_task = None
    try:
        # Imported here because the attachments service imports this module.
        from .services.attachments import prune_attachments
        pruned = await prune_attachments()
        if pruned["expired"] or pruned["orphaned"]:
            logger.info(
                "Pruned %s unsent and %s orphaned attachment(s) on startup",
                pruned["expired"], pruned["orphaned"])
    except Exception:
        logger.warning("Failed to prune attachments on startup", exc_info=True)

    try:
        presence = create_voice_presence(getattr(app.state, "comms", None))
        if presence is not None:
            presence_task = asyncio.create_task(presence.run())
    except Exception:
        logger.warning("Failed to start voice presence", exc_info=True)
    app.state.voice_presence = presence
    app.state.voice_moderation = VoiceModeration()

    stats_task = None
    try:
        comms = getattr(app.state, "comms", None)
        if sampling_enabled() and comms is not None:
            stats_task = asyncio.create_task(StatsSampler(comms).run())
    except Exception:
        logger.warning("Failed to start stats sampling", exc_info=True)
    app.state.stats_task = stats_task

    # Imported here because the attachments service imports this module.
    from .services.imports.runner import ImportRunner
    imports = ImportRunner(getattr(app.state, "comms", None))
    app.state.import_runner = imports
    try:
        await imports.prune()
    except Exception:
        logger.warning("Failed to prune imports on startup", exc_info=True)
    try:
        await imports.resume()
    except Exception:
        logger.warning("Failed to resume imports on startup", exc_info=True)

    yield

    try:
        await imports.shutdown()
    except Exception:
        logger.warning("Failed to stop imports", exc_info=True)

    if stats_task is not None:
        stats_task.cancel()
        with suppress(asyncio.CancelledError):
            await stats_task

    if presence_task is not None:
        presence_task.cancel()
        with suppress(asyncio.CancelledError):
            await presence_task
    if presence is not None:
        try:
            await presence.aclose()
        except Exception:
            logger.warning("Failed to close voice presence", exc_info=True)


def require_owner(user, server):
    """Enforce that *user* owns *server*; raise 403 otherwise.

    Ownership is stricter than membership: use this to gate destructive or
    server-wide writes (settings, deletion, invites) that only the owner may do.
    """
    if server.owner_id is None or server.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the server owner can do this")
