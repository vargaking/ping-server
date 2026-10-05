"""Unpack an uploaded export zip into a bundle folder.

The zip comes from an untrusted person. Only the files a bundle consists of
are written, each to a path built from checked parts, never from the entry's
own name. Everything else in the archive is ignored.
"""
import errno
import logging
import re
import shutil
import stat
import struct
import threading
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .storage import human_size

logger = logging.getLogger("app.services.imports")

MAX_ENTRIES = 500_000
MAX_JSON_BYTES = 64 * 1024 * 1024
FREE_SPACE_MARGIN = 256 * 1024 * 1024
BLOCK = 1024 * 1024
_MAX_DIRECTORY_BYTES_PER_ENTRY = 256

_ID = re.compile(r"[\w.-]{1,64}")
_CHUNK_NAME = re.compile(r"([0-9]{1,18})\.json")


def _too_big(limit: int) -> str:
    return f"This export is larger than the allowed unpacked size ({human_size(limit)})"


class UnpackError(Exception):
    """The zip can't be unpacked. The message is safe to show the owner."""


@dataclass
class _Entry:
    info: zipfile.ZipInfo
    parts: tuple[str, ...]
    is_json: bool


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def unpack_bundle(
    zip_path: Path,
    dest: Path,
    *,
    max_unpacked: int,
    max_file: int,
    stop: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Write the bundle in *zip_path* to *dest*. *max_unpacked* caps the bytes
    written, *max_file* is the attachment size limit: larger files are left
    out. *progress* is called with (bytes written, bytes to write). Setting
    *stop* ends the run with an UnpackError. Whatever fails, *dest* is removed."""
    try:
        _unpack(zip_path, dest, max_unpacked, max_file, stop, progress)
    except UnpackError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise UnpackError("Not enough disk space to unpack this export") from exc
        if isinstance(exc, (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError)):
            raise UnpackError("This zip is damaged or uses an unsupported format") from exc
        logger.warning("Unpacking an import failed", exc_info=True)
        raise UnpackError("The export couldn't be unpacked") from exc


def _unpack(zip_path, dest, max_unpacked, max_file, stop, progress) -> None:
    _check_directory_size(zip_path)
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, EOFError, ValueError):
        raise UnpackError("This file is not a zip archive") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnpackError("This zip has too many files")
        if any(info.flag_bits & 0x1 for info in infos):
            raise UnpackError("This zip is password protected")
        entries = _select(infos, max_file)
        total = sum(entry.info.file_size for entry in entries)
        _check_room(dest, entries, total, max_unpacked)
        writer = _Writer(archive, dest.resolve(), total, max_unpacked, stop, progress)
        writer.dest.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            writer.copy(entry)


def _check_directory_size(path: Path) -> None:
    """zipfile reads the whole central directory into memory, so refuse an
    absurd one before it does."""
    with open(path, "rb") as file:
        file.seek(0, 2)
        size = file.tell()
        tail_size = min(size, 65535 + 22)
        file.seek(size - tail_size)
        tail = file.read(tail_size)
        at = tail.rfind(b"PK\x05\x06")
        if at < 0 or len(tail) - at < 22:
            return
        entries, directory_size = struct.unpack_from("<HI", tail, at + 10)
        if entries == 0xFFFF or directory_size == 0xFFFFFFFF:
            locator = tail[max(at - 20, 0):at]
            if len(locator) == 20 and locator[:4] == b"PK\x06\x07":
                file.seek(struct.unpack_from("<Q", locator, 8)[0])
                record = file.read(56)
                if record[:4] == b"PK\x06\x06" and len(record) >= 48:
                    entries, directory_size = struct.unpack_from("<QQ", record, 32)
    if entries > MAX_ENTRIES or directory_size > MAX_ENTRIES * _MAX_DIRECTORY_BYTES_PER_ENTRY:
        raise UnpackError("This zip has too many files")


def _clean_name(name: str) -> tuple[str, ...] | None:
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        return None
    parts = tuple(name.split("/"))
    if any(part in ("", ".", "..") or "\0" in part for part in parts):
        return None
    return parts


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False
    # Writers that don't record a file type (Python's own, Windows ones) count as regular.
    return stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)


def _shape(parts: tuple[str, ...]) -> str | None:
    """"json" or "file" for the paths a bundle consists of, None for the rest."""
    if parts == ("server.json",):
        return "json"
    if len(parts) == 4 and parts[0] == "channels" and _valid_id(parts[1]):
        if parts[2] == "messages" and _CHUNK_NAME.fullmatch(parts[3]):
            return "json"
        if parts[2] == "threads" and parts[3].endswith(".json") and _valid_id(parts[3][:-5]):
            return "json"
    if len(parts) == 3 and parts[0] == "files" and _valid_id(parts[1]) and _valid_filename(parts[2]):
        return "file"
    return None


def _valid_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None and value not in (".", "..")


def _valid_filename(value: str) -> bool:
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 1 <= length <= 255 and value not in (".", "..") and "\0" not in value


def _select(infos: list[zipfile.ZipInfo], max_file: int) -> list[_Entry]:
    candidates = [
        (info, parts) for info in infos
        if _is_regular_file(info) and (parts := _clean_name(info.filename)) is not None]
    roots = [parts[:-1] for _, parts in candidates
             if parts[-1] == "server.json" and len(parts) <= 2]
    if not roots:
        raise UnpackError("This zip has no server.json")
    root = min(roots, key=len)

    entries: list[_Entry] = []
    seen = set()
    for info, parts in candidates:
        if parts[:len(root)] != root:
            continue
        relative = parts[len(root):]
        kind = _shape(relative)
        if kind is None or relative in seen:
            continue
        seen.add(relative)
        if kind == "file" and info.file_size > max_file:
            continue
        entries.append(_Entry(info, relative, is_json=kind == "json"))
    return entries


def _check_room(dest: Path, entries: list[_Entry], total: int, max_unpacked: int) -> None:
    if any(e.is_json and e.info.file_size > MAX_JSON_BYTES for e in entries):
        raise UnpackError(f"A data file in this zip is larger than {human_size(MAX_JSON_BYTES)}")
    if total > max_unpacked:
        raise UnpackError(_too_big(max_unpacked))
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    if total > free_bytes(parent) - FREE_SPACE_MARGIN:
        raise UnpackError("Not enough disk space to unpack this export")


class _Writer:
    def __init__(self, archive, dest, total, max_unpacked, stop, progress):
        self.archive = archive
        self.dest = dest
        self.total = total
        self.max_unpacked = max_unpacked
        self.stop = stop
        self.progress = progress
        self.written = 0

    def copy(self, entry: _Entry) -> None:
        target = self.dest.joinpath(*entry.parts)
        if not target.resolve().is_relative_to(self.dest):
            raise UnpackError("This zip contains a path that isn't allowed")
        target.parent.mkdir(parents=True, exist_ok=True)
        declared = entry.info.file_size
        copied = 0
        with self.archive.open(entry.info) as source, open(target, "xb") as out:
            while block := source.read(BLOCK):
                if self.stop is not None and self.stop.is_set():
                    raise UnpackError("Unpacking was stopped")
                copied += len(block)
                self.written += len(block)
                self._check(entry, copied, declared)
                out.write(block)
                if self.progress is not None:
                    self.progress(self.written, self.total)
        if copied != declared:
            raise zipfile.BadZipFile("short entry")

    def _check(self, entry: _Entry, copied: int, declared: int) -> None:
        if copied > declared:
            raise UnpackError("An entry in this zip is larger than it says")
        if entry.is_json and copied > MAX_JSON_BYTES:
            raise UnpackError(f"A data file in this zip is larger than {human_size(MAX_JSON_BYTES)}")
        if self.written > self.max_unpacked:
            raise UnpackError(_too_big(self.max_unpacked))
