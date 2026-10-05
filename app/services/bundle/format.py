"""Models and readers for a server bundle, format version 1."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Iterator

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

SUPPORTED_FORMAT = 1


class BundleError(ValueError):
    """The bundle can't be read."""


def _as_id(value):
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


Id = Annotated[str, BeforeValidator(_as_id)]
Timestamp = Annotated[datetime, BeforeValidator(lambda v: _utc(datetime.fromisoformat(v)) if isinstance(v, str) else v)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Source(_Model):
    platform: str
    server_id: Id
    server_name: str = ""
    exported_at: str | None = None


class Category(_Model):
    id: Id
    name: str
    position: int = 0


class Tag(_Model):
    id: Id
    name: str


class ChannelInfo(_Model):
    id: Id
    name: str
    type: str = "text"
    topic: str | None = None
    category_id: Id | None = None
    position: int = 0
    private: bool = False
    tags: list[Tag] = []


class Author(_Model):
    id: Id
    name: str
    avatar: str | None = None
    messages: int = 0


class Emoji(_Model):
    id: Id
    name: str
    path: str | None = None


class Unreadable(_Model):
    id: Id
    name: str = ""


class ServerBundle(_Model):
    format: int
    source: Source
    categories: list[Category] = []
    channels: list[ChannelInfo] = []
    authors: list[Author] = []
    emoji: list[Emoji] = []
    unreadable: list[Unreadable] = []


class Attachment(_Model):
    id: Id
    filename: str = "file"
    size: int = 0
    content_type: str | None = None
    path: str | None = None


class Embed(_Model):
    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    image_url: str | None = None


class Reaction(_Model):
    emoji: str
    count: int = 0


class Message(_Model):
    id: Id
    author_id: Id
    timestamp: Timestamp
    edited_at: Timestamp | None = None
    content: str = ""
    reply_to_id: Id | None = None
    pinned: bool = False
    attachments: list[Attachment] = []
    embeds: list[Embed] = []
    reactions: list[Reaction] = []


class Thread(_Model):
    id: Id
    title: str
    tag_ids: list[Id] = []
    pinned: bool = False
    locked: bool = False
    created_at: Timestamp
    messages: list[Message] = []


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BundleError(f"{path} is missing") from None
    except (OSError, ValueError) as exc:
        raise BundleError(f"{path} can't be read: {exc}") from exc


def _validated(model, raw, path: Path):
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise BundleError(f"{path} is not valid: {exc}") from exc


def load_server(bundle: Path) -> ServerBundle:
    """server.json, or BundleError if it is missing, unreadable or not a
    format this importer reads."""
    path = bundle / "server.json"
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise BundleError(f"{path} is not an object")
    if raw.get("format") != SUPPORTED_FORMAT:
        raise BundleError(
            f"Unsupported bundle format {raw.get('format')!r}; "
            f"this importer reads format {SUPPORTED_FORMAT}")
    return _validated(ServerBundle, raw, path)


def _channel_dir(bundle: Path, channel_id: str, kind: str) -> Path:
    if not re.fullmatch(r"[\w.-]+", channel_id) or channel_id in (".", ".."):
        raise BundleError(f"Channel id {channel_id!r} can't name a folder")
    return bundle / "channels" / channel_id / kind


def _numbered(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir() if re.fullmatch(r"\d+\.json", p.name)]
    return sorted(files, key=lambda p: int(p.stem))


def iter_channel_messages(bundle: Path, channel_id: str) -> Iterator[list[Message]]:
    """The messages of each chunk file, oldest chunk first. One chunk is held
    at a time."""
    for path in _numbered(_channel_dir(bundle, channel_id, "messages")):
        raw = _read_json(path)
        if not isinstance(raw, list):
            raise BundleError(f"{path} is not an array")
        yield [_validated(Message, item, path) for item in raw]


def thread_files(bundle: Path, channel_id: str) -> list[Path]:
    directory = _channel_dir(bundle, channel_id, "threads")
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"), key=lambda p: (len(p.stem), p.stem))


def load_thread(path: Path) -> Thread:
    return _validated(Thread, _read_json(path), path)


def resolve_file(bundle: Path, relative: str | None) -> Path | None:
    """The file a bundle path names, or None if there is none inside the bundle."""
    if not relative:
        return None
    root = bundle.resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None
