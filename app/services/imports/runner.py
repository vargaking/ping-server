"""Runs the jobs of server imports: unpack and check an upload, import it, hand
imported messages to newly mapped authors. One job at a time on the whole
instance; the rest wait their turn.

A runner belongs to one app start (see utils.lifespan): it keeps asyncio state.
"""
import asyncio
import logging
import threading
import time
import weakref
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import UUID

import anyio

from ...models.Server import Server
from ...models.ServerImport import ServerImport
from ...models.User import User
from ...models.UserToServer import UserToServer
from ...settings import import_max_unpacked_bytes, imports_root
from ..attachments import max_attachment_bytes
from ..bundle import format as bundle_format
from ..bundle.format import BundleError
from ..bundle.importer import ImportAborted, ImportOptions, import_bundle
from ..bundle.plan import plan_json
from . import storage
from .serialize import import_json
from .unpack import UnpackError, unpack_bundle

logger = logging.getLogger("app.services.imports")

PROGRESS_INTERVAL = 1.0
STALE_UPLOAD = timedelta(hours=24)
SHUTDOWN_WAIT = 5.0
UNEXPECTED_ERROR = "The import failed unexpectedly"
MAX_ERROR_LENGTH = 1000

_RUNNING_STATUS = {"unpack": "unpacking", "import": "importing", "authors": "importing"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Job:
    """What a running job tells the owner: progress, at most once a second,
    and every change of status."""

    def __init__(self, runner: "ImportRunner", import_id: UUID, server_id: int,
                 owner_id: int | None, status: str):
        self.runner = runner
        self.import_id = import_id
        self.server_id = server_id
        self.owner_id = owner_id
        self.status = status
        self._last = float("-inf")

    async def progress(
        self, phase: str, done: int, total: int, label: str | None = None, *, force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now
        await self.change(
            self.status, progress={"phase": phase, "done": done, "total": total, "label": label})

    async def change(self, expected: str, **fields) -> bool:
        """Update the row if it is still in status *expected* and tell the
        owner. False when the row is gone or moved on."""
        updated = await ServerImport.filter(id=self.import_id, status=expected).update(
            updated_at=_now(), **fields)
        if not updated:
            return False
        await self._announce()
        return True

    async def _announce(self) -> None:
        comms = self.runner.comms
        if comms is None or self.owner_id is None:
            return
        row = await ServerImport.get_or_none(id=self.import_id)
        if row is None:
            return
        await comms.send_to_user(self.owner_id, {
            "type": "server_import_updated", "server_id": self.server_id,
            "import": import_json(row, light=True)})


class ImportRunner:
    def __init__(self, comms=None):
        self.comms = comms
        self._lock = asyncio.Lock()
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._upload_locks: weakref.WeakValueDictionary[UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary())

    # Entry points

    def upload_lock(self, import_id: UUID) -> asyncio.Lock:
        """One writer per upload at a time."""
        lock = self._upload_locks.get(import_id)
        if lock is None:
            lock = self._upload_locks[import_id] = asyncio.Lock()
        return lock

    def start_unpack(self, import_id: UUID) -> bool:
        return self._spawn(import_id, "unpack")

    def start_import(self, import_id: UUID) -> bool:
        return self._spawn(import_id, "import")

    def start_handover(self, import_id: UUID) -> bool:
        return self._spawn(import_id, "authors")

    def is_running(self, import_id: UUID) -> bool:
        return import_id in self._tasks

    async def wait(self, import_id: UUID) -> None:
        task = self._tasks.get(import_id)
        if task is not None:
            await asyncio.wait([task])

    async def stop(self, import_ids) -> None:
        """Cancel the running jobs of these imports."""
        tasks = [task for i in import_ids if (task := self._tasks.get(i)) is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT)

    # Startup

    async def prune(self) -> None:
        """Drop uploads nobody finished and folders no import owns."""
        stale = await ServerImport.filter(
            status="uploading", updated_at__lt=_now() - STALE_UPLOAD).delete()
        known = set(await ServerImport.all().values_list("id", flat=True))
        orphans = await anyio.to_thread.run_sync(storage.orphaned_dirs, known)
        for path in orphans:
            await anyio.to_thread.run_sync(storage.remove_path, path)
        if stale or orphans:
            logger.info("Pruned %s stale upload(s) and %s orphaned import folder(s)",
                        stale, len(orphans))

    async def resume(self) -> None:
        """Restart what a shutdown interrupted."""
        for row in await ServerImport.filter(status__in=["unpacking", "importing"]):
            if row.status == "unpacking":
                self.start_unpack(row.id)
            elif (row.progress or {}).get("phase") == "authors" or row.result is not None:
                self.start_handover(row.id)
            else:
                self.start_import(row.id)

    # Jobs

    def _spawn(self, import_id: UUID, kind: str) -> bool:
        if import_id in self._tasks:
            return False
        task = asyncio.create_task(self._run(import_id, kind), name=f"import-{kind}-{import_id}")
        self._tasks[import_id] = task
        task.add_done_callback(partial(self._finished, import_id))
        return True

    def _finished(self, import_id: UUID, task: asyncio.Task) -> None:
        if self._tasks.get(import_id) is task:
            del self._tasks[import_id]

    async def _run(self, import_id: UUID, kind: str) -> None:
        row = await ServerImport.get_or_none(id=import_id)
        if row is None:
            return
        server = await Server.get_or_none(id=row.server_id)
        job = _Job(self, import_id, row.server_id, server.owner_id if server else None,
                   _RUNNING_STATUS[kind])
        try:
            if self._lock.locked():
                await job.progress("queued", 0, 0, force=True)
            async with self._lock:
                await getattr(self, f"_{kind}")(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(job, kind, exc)

    async def _unpack(self, job: _Job) -> None:
        row = await ServerImport.get_or_none(id=job.import_id)
        if row is None or row.status != "unpacking":
            return
        await job.progress("unpacking", 0, 0, force=True)
        bundle = storage.bundle_path(job.import_id)
        upload = storage.zip_path(job.import_id)
        if upload.is_file():
            await anyio.to_thread.run_sync(storage.remove_path, bundle)
            await self._unpack_zip(job, upload, bundle)
            await anyio.to_thread.run_sync(storage.remove_path, upload)
        elif not (bundle / "server.json").is_file():
            raise UnpackError("The upload is no longer on this server; upload it again")
        await self._check(job, row)

    async def _unpack_zip(self, job: _Job, upload, bundle) -> None:
        stop = threading.Event()
        state = [0, 0]

        def report(done: int, total: int) -> None:
            state[:] = [done, total]

        async def watch() -> None:
            while True:
                await anyio.sleep(PROGRESS_INTERVAL / 2)
                await job.progress("unpacking", *state)

        work = partial(
            unpack_bundle, upload, bundle, max_unpacked=import_max_unpacked_bytes(),
            max_file=max_attachment_bytes(), stop=stop, progress=report)
        watcher = asyncio.create_task(watch())
        try:
            await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
        finally:
            stop.set()
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    async def _check(self, job: _Job, row: ServerImport) -> None:
        bundle = storage.bundle_path(job.import_id)
        server = await anyio.to_thread.run_sync(bundle_format.load_server, bundle)
        source = server.source
        mapping = await self._inherited_mapping(row, f"{source.platform}:{source.server_id}")
        usernames, _ = await self._usernames(mapping)

        async def report_progress(seen: int, channel: str | None) -> None:
            await job.progress("checking", seen, 0, channel)

        report = await import_bundle(
            bundle, ImportOptions(server_id=row.server_id, authors=usernames, dry_run=True),
            report_progress)
        plan = plan_json(report)
        plan["authors"] = [
            {"id": a.id, "name": a.name, "messages": a.messages} for a in report.authors]
        plan["seen"] = report.seen
        known = {a.id for a in report.authors}
        await job.change(
            "unpacking", status="ready", progress=None, error=None, failed_step=None,
            plan=plan, authors={a: u for a, u in mapping.items() if a in known},
            source=report.source[:200], source_platform=source.platform[:50],
            source_name=source.server_name[:200])

    @staticmethod
    async def _inherited_mapping(row: ServerImport, source: str) -> dict:
        """The authors an earlier finished import of the same source was mapped
        to, as far as those users are still members."""
        earlier = await ServerImport.filter(
            server_id=row.server_id, status="done", source=source,
        ).exclude(id=row.id).order_by("-created_at").first()
        if earlier is None:
            return dict(row.authors or {})
        members = set(await UserToServer.filter(
            server_id=row.server_id).values_list("user_id", flat=True))
        return {a: u for a, u in (earlier.authors or {}).items() if u in members}

    @staticmethod
    async def _usernames(mapping: dict) -> tuple[dict[str, str], dict]:
        """The mapping as the importer wants it, and the mapping without the
        users that no longer exist."""
        names = dict(await User.filter(id__in=set(mapping.values())).values_list("id", "username"))
        kept = {a: u for a, u in mapping.items() if u in names}
        return {a: names[u] for a, u in kept.items()}, kept

    async def _import(self, job: _Job) -> None:
        row = await self._importing_row(job)
        if row is None:
            return
        report = await self._run_importer(job, row, "importing", ImportOptions(
            server_id=row.server_id, include_private=False))
        await anyio.to_thread.run_sync(storage.remove_path, storage.bundle_path(row.id) / "files")
        await self._done(job, result=plan_json(report))

    async def _authors(self, job: _Job) -> None:
        row = await self._importing_row(job)
        if row is None:
            return
        await self._run_importer(job, row, "authors", ImportOptions(
            server_id=row.server_id, existing_only=True))
        await self._done(job)

    @staticmethod
    async def _importing_row(job: _Job) -> ServerImport | None:
        row = await ServerImport.get_or_none(id=job.import_id)
        return row if row is not None and row.status == "importing" else None

    async def _run_importer(self, job: _Job, row: ServerImport, phase: str, options: ImportOptions):
        usernames, kept = await self._usernames(row.authors or {})
        if kept != (row.authors or {}):
            await ServerImport.filter(id=row.id).update(authors=kept)
        options.authors = usernames
        total = (row.plan or {}).get("seen", 0)
        await job.progress(phase, 0, total, force=True)

        async def report_progress(seen: int, channel: str | None) -> None:
            await job.progress(phase, seen, total, channel)

        return await import_bundle(storage.bundle_path(row.id), options, report_progress)

    async def _done(self, job: _Job, **fields) -> None:
        changed = await job.change(
            "importing", status="done", progress=None, error=None, failed_step=None, **fields)
        if changed and self.comms is not None:
            await self.comms.broadcast_to_server(
                job.server_id, {"type": "server_import_finished", "server_id": job.server_id})

    async def _fail(self, job: _Job, kind: str, exc: Exception) -> None:
        if isinstance(exc, (UnpackError, BundleError, ImportAborted)):
            message = self._scrub(str(exc), job.import_id)
        else:
            logger.error("Import %s failed", job.import_id, exc_info=exc)
            message = UNEXPECTED_ERROR
        try:
            if kind == "authors":
                await job.change("importing", status="done", progress=None, error=message)
            else:
                await job.change(
                    job.status, status="failed", progress=None, error=message,
                    failed_step=_RUNNING_STATUS[kind])
            if kind == "unpack":
                await anyio.to_thread.run_sync(storage.remove_import_dir, job.import_id)
        except Exception:
            logger.warning("Couldn't record the failure of import %s", job.import_id, exc_info=True)

    @staticmethod
    def _scrub(message: str, import_id: UUID) -> str:
        """Importer messages name files by their full path; the owner only
        needs the part inside the bundle."""
        bundle = str(storage.bundle_path(import_id))
        for path in (bundle, str(storage.import_dir(import_id)), str(imports_root())):
            message = message.replace(path + "/", "").replace(path, "bundle")
        return message[:MAX_ERROR_LENGTH]
