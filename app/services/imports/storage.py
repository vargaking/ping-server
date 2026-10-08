"""Where an import keeps its files: IMPORTS_ROOT/<import id>/upload.zip and bundle/."""
import os
import shutil
from pathlib import Path
from uuid import UUID

import anyio

from ...settings import imports_root


def human_size(size: int) -> str:
    for unit, unit_size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= unit_size:
            return f"{size / unit_size:.1f}".removesuffix(".0") + f" {unit}"
    return f"{size} bytes"


def import_dir(import_id: UUID) -> Path:
    return imports_root() / str(import_id)


def zip_path(import_id: UUID) -> Path:
    return import_dir(import_id) / "upload.zip"


def bundle_path(import_id: UUID) -> Path:
    return import_dir(import_id) / "bundle"


def received_bytes(import_id: UUID) -> int:
    try:
        return zip_path(import_id).stat().st_size
    except FileNotFoundError:
        return 0


def append_piece(import_id: UUID, offset: int, data: bytes) -> int | None:
    """Add *data* to the upload if the upload is exactly *offset* bytes long.
    Returns the new length, or None when the offset is wrong. A failed write
    leaves the file as it was."""
    path = zip_path(import_id)
    if offset != received_bytes(import_id):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "ab") as upload:
            upload.write(data)
    except OSError:
        if path.exists():
            os.truncate(path, offset)
        raise
    return offset + len(data)


def remove_import_dir(import_id: UUID) -> None:
    shutil.rmtree(import_dir(import_id), ignore_errors=True)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def orphaned_dirs(known: set[UUID]) -> list[Path]:
    """Import folders under IMPORTS_ROOT that no row owns. Anything whose name
    is not an import id is left alone."""
    found = []
    for entry in imports_root().iterdir():
        try:
            import_id = UUID(entry.name)
        except ValueError:
            continue
        if str(import_id) == entry.name and entry.is_dir() and import_id not in known:
            found.append(entry)
    return found


async def remove_import_dirs(import_ids) -> None:
    """Delete the folders of imports, without blocking the event loop."""
    for import_id in import_ids:
        await anyio.to_thread.run_sync(remove_import_dir, import_id)
