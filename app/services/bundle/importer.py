"""Import a server bundle (see docs/bundle-format.md) into an existing server.

The import writes straight to the database: no push, no WebSocket fan-out and
no unread marking. Every channel, post and message carries an import marker,
so a rerun skips what exists and a refreshed bundle only adds what is new.
"""
import json
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid5

import anyio
from pydantic import ValidationError
from tortoise.functions import Count, Max
from tortoise.transactions import in_transaction

from ...models.Attachment import Attachment
from ...models.Channel import Channel
from ...models.ChannelGroup import ChannelGroup
from ...models.ForumPost import ForumPost
from ...models.ForumPostTag import ForumPostTag
from ...models.ForumTag import ForumTag
from ...models.Message import Message
from ...models.ReadState import ReadState
from ...models.Server import Server
from ...models.User import User
from ...models.UserToServer import UserToServer
from ...ws_schemas import MAX_EMBEDS_PER_MESSAGE, EmbedIn
from .. import channel_layout
from ..attachments import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    attachments_root,
    declared_content_type,
    max_attachment_bytes,
    sanitize_filename,
    sniff_image,
    store,
)
from ..forum import MAX_TAGS_PER_CHANNEL, MAX_TAGS_PER_POST, TAG_NAME_MAX, TITLE_MAX
from ..message_content import InvalidContent, normalize_content, serialize
from ..permissions import permissions
from ..system_user import IMPORTED_PASSWORD_HASH, IMPORTED_USERNAME
from . import format as bundle_format
from .format import BundleError, ChannelInfo
from .markdown import to_doc

BATCH_SIZE = 500
EXAMPLE_LIMIT = 20
_ID_CHUNK = 500
_NAMESPACE = UUID("5c1d8e0a-3b7f-4a52-9d0e-6f21c4b8a7e3")
_NAME_MAX = 100
_TOPIC_MAX = 1024


class ImportAborted(Exception):
    """The import can't run as asked; nothing was written for this reason."""


@dataclass
class ImportOptions:
    server_id: int
    authors: dict[str, str] = field(default_factory=dict)
    only: set[str] = field(default_factory=set)
    channel_map: dict[str, int] = field(default_factory=dict)
    include_private: bool = False
    dry_run: bool = False
    # Only hand existing messages to mapped authors. Creates nothing but the import account.
    existing_only: bool = False


Progress = Callable[[int, str | None], Awaitable[None]]


@dataclass
class ChannelReport:
    source_id: str
    name: str
    type: str
    plan: str
    action: str = "create"
    reason: str | None = None
    target_name: str | None = None
    matched_by: str | None = None
    channel_id: int | None = None
    category: str | None = None
    messages: int = 0
    existing_messages: int = 0
    posts: int = 0
    existing_posts: int = 0
    attachments: int = 0
    attachment_bytes: int = 0
    handed_over: int = 0


@dataclass
class LeftOut:
    private_channels: int = 0
    unreadable_channels: int = 0
    text_channel_threads: int = 0
    messages_with_reactions: int = 0
    pinned_messages: int = 0
    voice_text_chat: int = 0
    emoji: int = 0
    avatars: int = 0


@dataclass
class UnmappedAuthor:
    id: str
    name: str
    messages: int


@dataclass
class SeenAuthor:
    id: str
    name: str
    messages: int


@dataclass
class Report:
    source: str
    dry_run: bool
    free_bytes: int = 0
    seen: int = 0
    channels: list[ChannelReport] = field(default_factory=list)
    over_cap: int = 0
    over_cap_examples: list[str] = field(default_factory=list)
    missing: int = 0
    missing_examples: list[str] = field(default_factory=list)
    over_attachment_limit: int = 0
    empty_skipped: int = 0
    invalid_skipped: int = 0
    tags_over_limit: int = 0
    left_out: LeftOut = field(default_factory=LeftOut)
    authors: list[SeenAuthor] = field(default_factory=list)
    unmapped_authors: list[UnmappedAuthor] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def messages(self) -> int:
        return sum(c.messages for c in self.channels)

    @property
    def posts(self) -> int:
        return sum(c.posts for c in self.channels)

    @property
    def attachments(self) -> int:
        return sum(c.attachments for c in self.channels)

    @property
    def attachment_bytes(self) -> int:
        return sum(c.attachment_bytes for c in self.channels)


def read_authors(path: Path) -> dict[str, str]:
    """authors.json: source author id to Zet username."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportAborted(f"{path} can't be read: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise ImportAborted(f"{path} must map source author ids to usernames")
    return {str(k): v for k, v in raw.items()}


def message_uuid(server_id: int, source: str, message_id: str) -> UUID:
    return uuid5(_NAMESPACE, f"{server_id}:{source}:message:{message_id}")


def _marker(settings_or_metadata: dict | None) -> dict:
    marker = (settings_or_metadata or {}).get("import")
    return marker if isinstance(marker, dict) else {}


@dataclass
class _Plan:
    info: ChannelInfo
    report: ChannelReport
    skipped: bool = False
    channel: Channel | None = None
    category_name: str | None = None


@dataclass
class _PostState:
    thread: "bundle_format.Thread"
    owner_id: int | None
    imported_author: dict | None
    post_id: int | None = None
    opening_id: int | None = None
    tag_ids: list[int] = field(default_factory=list)


@dataclass
class _Prepared:
    source: bundle_format.Message
    uuid: UUID
    owner_id: int | None
    imported_author: dict | None
    content: dict
    embeds: list[dict]
    attachments: list[dict]
    reply_uuid: UUID | None


def _chunks(items, size: int = _ID_CHUNK):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


class _Import:
    def __init__(self, bundle: Path, options: ImportOptions, progress: Progress | None = None):
        self.path = bundle
        self.options = options
        self.progress = progress
        self.dry = options.dry_run
        self.server = bundle_format.load_server(bundle)
        self.source = f"{self.server.source.platform}:{self.server.source.server_id}"
        self.report = Report(source=self.source, dry_run=self.dry)
        self.names = {a.id: a.name for a in self.server.authors}
        self.users: dict[str, User] = {}
        self.author_counts: Counter = Counter()
        self.system_id: int | None = None
        self.groups: dict[str, ChannelGroup] = {}

    async def run(self) -> Report:
        await self._resolve_authors()
        await self._check_system_user()
        plans = await self._plan()
        for plan in plans:
            if not plan.skipped:
                await self._run_channel(plan)
        self._finish()
        return self.report

    async def _resolve_authors(self) -> None:
        wanted = set(self.options.authors.values())
        if IMPORTED_USERNAME in wanted:
            raise ImportAborted(f"{IMPORTED_USERNAME} can't be mapped to an author")
        found = {u.username: u for u in await User.filter(username__in=wanted)}
        unknown = sorted(wanted - set(found))
        if unknown:
            raise ImportAborted("Unknown usernames in the authors file: " + ", ".join(unknown))
        self.users = {a: found[name] for a, name in self.options.authors.items()}
        members = set(await UserToServer.filter(
            server_id=self.options.server_id,
            user_id__in=[u.id for u in found.values()]).values_list("user_id", flat=True))
        for username, user in sorted(found.items()):
            if user.id not in members:
                self.report.warnings.append(f"{username} is mapped but is not a member of the server")

    async def _check_system_user(self) -> None:
        user = await User.get_or_none(username=IMPORTED_USERNAME)
        if user is None:
            return
        if user.password_hash != IMPORTED_PASSWORD_HASH:
            raise ImportAborted(
                f"A user named {IMPORTED_USERNAME} already exists and is not the import account")
        self.system_id = user.id

    async def _system_user_id(self) -> int | None:
        if self.system_id is None and not self.dry:
            user = await User.create(
                username=IMPORTED_USERNAME, password_hash=IMPORTED_PASSWORD_HASH, profile={})
            self.system_id = user.id
        return self.system_id

    async def _owner(self, author_id: str) -> tuple[int | None, dict | None]:
        if author_id in self.users:
            return self.users[author_id].id, None
        imported = {"id": author_id, "name": self.names.get(author_id, author_id)}
        return await self._system_user_id(), imported

    def _resolve_mention(self, author_id: str) -> tuple:
        user = self.users.get(author_id)
        if user is not None:
            return ("mention", user.id, user.username)
        name = self.names.get(author_id)
        return ("text", f"@{name}" if name else "@unknown")

    # Plan

    async def _plan(self) -> list[_Plan]:
        bundle_channels = {c.id: c for c in self.server.channels}
        for source_id in [*self.options.only, *self.options.channel_map]:
            if source_id not in bundle_channels:
                raise ImportAborted(f"Channel {source_id} is not in the bundle")

        existing = await Channel.filter(server_id=self.options.server_id)
        by_id = {c.id: c for c in existing}
        targets: dict[str, Channel] = {}
        for source_id, target_id in self.options.channel_map.items():
            target = by_id.get(target_id)
            info = bundle_channels[source_id]
            if target is None:
                raise ImportAborted(
                    f"--map {source_id}={target_id}: no such channel in this server")
            if target.type != info.type:
                raise ImportAborted(
                    f"--map {source_id}={target_id}: #{target.name} is a {target.type} "
                    f"channel, the source is {info.type}")
            targets[source_id] = target

        plans = [self._plan_for(info) for info in self._in_source_order()]
        matched = {p.info.id: (t, "map") for p in plans if not p.skipped
                   if (t := targets.get(p.info.id))}
        claimed = {t.id for t, _ in matched.values()}
        for plan in plans:
            if plan.skipped or plan.info.id in matched:
                continue
            for channel in existing:
                marker = _marker(channel.channel_settings)
                if (marker.get("source") == self.source and marker.get("id") == plan.info.id
                        and channel.id not in claimed):
                    matched[plan.info.id] = (channel, "earlier import")
                    claimed.add(channel.id)
                    break
        for plan in plans:
            if plan.skipped or plan.info.id in matched:
                continue
            name = self._channel_name(plan.info)
            for channel in existing:
                if (channel.name == name and channel.type == plan.info.type
                        and channel.id not in claimed):
                    matched[plan.info.id] = (channel, "name")
                    claimed.add(channel.id)
                    break

        categories = {c.id: c for c in self.server.categories}
        new_groups = set()
        for plan in plans:
            if plan.skipped:
                continue
            found = matched.get(plan.info.id)
            if found:
                plan.channel, plan.report.matched_by = found
                plan.report.plan = f"into existing #{plan.channel.name}"
                plan.report.channel_id = plan.channel.id
                plan.report.action = "existing"
                plan.report.target_name = plan.channel.name
            elif self.options.existing_only:
                plan.skipped = True
                plan.report.plan = "skipped: not imported"
                plan.report.action = "skipped"
                plan.report.reason = "not imported"
                continue
            else:
                plan.report.plan = "create"
            category = categories.get(plan.info.category_id)
            plan.category_name = category.name.strip()[:_NAME_MAX] if category else None
            plan.report.category = plan.category_name
            if found is None and plan.category_name:
                new_groups.add(plan.category_name)

        creating = sum(1 for p in plans if not p.skipped and p.channel is None)
        if len(existing) + creating > channel_layout.MAX_CHANNELS:
            raise ImportAborted(
                f"The import would create {creating} channels; a server can have at most "
                f"{channel_layout.MAX_CHANNELS}")
        groups = await ChannelGroup.filter(server_id=self.options.server_id)
        self.groups = {}
        for group in groups:
            self.groups.setdefault(group.name, group)
        missing_groups = new_groups - set(self.groups)
        if len(groups) + len(missing_groups) > channel_layout.MAX_GROUPS:
            raise ImportAborted(
                f"The import would create {len(missing_groups)} categories; a server can have "
                f"at most {channel_layout.MAX_GROUPS}")
        self.report.channels = [p.report for p in plans]
        return plans

    def _in_source_order(self) -> list[ChannelInfo]:
        categories = {c.id: (c.position, index) for index, c in enumerate(self.server.categories)}

        def key(info: ChannelInfo):
            if info.category_id in categories:
                return (*categories[info.category_id], info.position)
            return (-1, -1, info.position)

        return sorted(self.server.channels, key=key)

    def _plan_for(self, info: ChannelInfo) -> _Plan:
        report = ChannelReport(
            source_id=info.id, name=info.name, type=info.type, plan="")
        plan = _Plan(info=info, report=report)
        unreadable = {u.id for u in self.server.unreadable}
        reason = shown = None
        if self.options.only and info.id not in self.options.only:
            reason, shown = "not in --only", "not selected"
        elif info.id in unreadable:
            reason = shown = "unreadable"
        elif info.private and not self.options.include_private:
            reason = shown = "private"
            if not self.options.existing_only:
                self.report.left_out.private_channels += 1
        elif info.type not in ("text", "voice", "forum"):
            reason = shown = f"unsupported type {info.type}"
        if reason:
            plan.skipped = True
            report.plan = f"skipped: {reason}"
            report.action = "skipped"
            report.reason = shown
        return plan

    @staticmethod
    def _channel_name(info: ChannelInfo) -> str:
        return info.name.strip()[:_NAME_MAX] or "channel"

    # Channels

    async def _ensure_channel(self, plan: _Plan) -> Channel | None:
        marker = {"source": self.source, "id": plan.info.id}
        if plan.channel is not None:
            if not self.dry and not self.options.existing_only and not _marker(
                    plan.channel.channel_settings):
                plan.channel.channel_settings = {
                    **plan.channel.channel_settings, "import": marker}
                await plan.channel.save(update_fields=["channel_settings"])
            return plan.channel
        if self.dry:
            return None
        server_id = self.options.server_id
        async with channel_layout.locked_server(server_id):
            group = await self._group(plan.category_name)
            group_id = group.id if group else None
            topic = (plan.info.topic or "").strip()[:_TOPIC_MAX] or None
            plan.channel = await Channel.create(
                name=self._channel_name(plan.info),
                type=plan.info.type,
                topic=topic,
                server_id=server_id,
                group_id=group_id,
                position=await channel_layout.next_channel_position(server_id, group_id),
                channel_settings={"import": {**marker, "created": True}},
            )
        permissions.invalidate(server_id)
        plan.report.channel_id = plan.channel.id
        return plan.channel

    async def _group(self, name: str | None) -> ChannelGroup | None:
        if name is None:
            return None
        if name not in self.groups:
            server_id = self.options.server_id
            self.groups[name] = await ChannelGroup.create(
                server_id=server_id, name=name,
                position=await channel_layout.next_group_position(server_id))
            permissions.invalidate(server_id)
        return self.groups[name]

    async def _run_channel(self, plan: _Plan) -> None:
        channel = await self._ensure_channel(plan)
        info = plan.info
        if info.type == "voice":
            if not self.options.existing_only:
                self.report.left_out.voice_text_chat += await anyio.to_thread.run_sync(
                    self._count_messages, info.id)
            return
        created = plan.channel is None or bool(_marker(plan.channel.channel_settings).get("created"))
        async with self._unread_guard(channel, created):
            if info.type == "text":
                await self._import_text(plan, channel)
            else:
                await self._import_forum(plan, channel)

    def _count_messages(self, channel_id: str) -> int:
        return sum(len(chunk) for chunk in bundle_format.iter_channel_messages(self.path, channel_id))

    async def _import_text(self, plan: _Plan, channel: Channel | None) -> None:
        info = plan.info
        if not self.options.existing_only:
            self.report.left_out.text_channel_threads += len(
                await anyio.to_thread.run_sync(bundle_format.thread_files, self.path, info.id))
        buffer: list = []
        chunks = bundle_format.iter_channel_messages(self.path, info.id)
        while (chunk := await anyio.to_thread.run_sync(next, chunks, None)) is not None:
            buffer.extend(chunk)
            while len(buffer) >= BATCH_SIZE:
                await self._write_batch(plan, channel, buffer[:BATCH_SIZE])
                del buffer[:BATCH_SIZE]
        if buffer:
            await self._write_batch(plan, channel, buffer)

    # Forums

    async def _import_forum(self, plan: _Plan, channel: Channel | None) -> None:
        existing_only = self.options.existing_only
        tag_ids = {} if existing_only else await self._ensure_tags(plan, channel)
        posts = await self._existing_posts(channel)
        for path in await anyio.to_thread.run_sync(
                bundle_format.thread_files, self.path, plan.info.id):
            thread = await anyio.to_thread.run_sync(bundle_format.load_thread, path)
            existing = posts.get(thread.id)
            if existing is None and existing_only:
                continue
            await self._import_thread(plan, channel, thread, existing, tag_ids)

    async def _ensure_tags(self, plan: _Plan, channel: Channel | None) -> dict[str, int]:
        existing = {}
        if channel is not None:
            existing = {t.name: t for t in await ForumTag.filter(channel_id=channel.id)}
        ids = {}
        missing: dict[str, list[str]] = {}
        for tag in plan.info.tags:
            name = tag.name.strip()[:TAG_NAME_MAX]
            if not name:
                continue
            if name in existing:
                ids[tag.id] = existing[name].id
            else:
                missing.setdefault(name, []).append(tag.id)
        room = max(0, MAX_TAGS_PER_CHANNEL - len(existing))
        for position, (name, source_ids) in enumerate(missing.items()):
            if position >= room:
                self.report.tags_over_limit += 1
                continue
            if not self.dry:
                created = await ForumTag.create(
                    channel_id=channel.id, name=name, position=len(existing) + position)
                for source_id in source_ids:
                    ids[source_id] = created.id
        return ids

    @staticmethod
    async def _existing_posts(channel: Channel | None) -> dict[str, dict]:
        if channel is None:
            return {}
        rows = await ForumPost.filter(channel_id=channel.id).values(
            "id", "metadata", "opening_message_id")
        return {_marker(r["metadata"]).get("id"): r for r in rows}

    async def _import_thread(
        self, plan: _Plan, channel: Channel | None, thread, existing: dict | None,
        tag_ids: dict[str, int],
    ) -> None:
        report = plan.report
        first = thread.messages[0] if thread.messages else None
        owner_id, imported = (None, None)
        if first is not None:
            owner_id, imported = await self._owner(first.author_id)
        post = _PostState(
            thread=thread, owner_id=owner_id, imported_author=imported,
            tag_ids=list(dict.fromkeys(
                tag_ids[t] for t in thread.tag_ids if t in tag_ids))[:MAX_TAGS_PER_POST])
        if existing is None:
            report.posts += 1
        else:
            report.existing_posts += 1
            post.post_id = existing["id"]
            post.opening_id = existing["opening_message_id"]

        for start in range(0, len(thread.messages), BATCH_SIZE):
            await self._write_batch(
                plan, channel, thread.messages[start:start + BATCH_SIZE], post)
        if self.dry or self.options.existing_only:
            return
        if post.post_id is None:
            async with in_transaction():
                await self._create_post(channel, post)
        await self._refresh_post(post)

    async def _create_post(self, channel: Channel, post: _PostState) -> None:
        thread = post.thread
        metadata = {"import": {"source": self.source, "id": thread.id}}
        if post.imported_author:
            metadata["imported_author"] = post.imported_author
        created = await ForumPost.create(
            channel_id=channel.id,
            author_id=post.owner_id,
            title=thread.title.strip()[:TITLE_MAX] or "Untitled",
            pinned=thread.pinned,
            locked=thread.locked,
            created_at=thread.created_at,
            last_activity_at=thread.created_at,
            metadata=metadata,
        )
        post.post_id = created.id
        await ForumPostTag.bulk_create(
            [ForumPostTag(post_id=created.id, tag_id=t) for t in post.tag_ids])

    @staticmethod
    async def _refresh_post(post: _PostState) -> None:
        totals = await Message.filter(post_id=post.post_id).annotate(
            count=Count("id"), last=Max("timestamp")).values("count", "last")
        count, last = totals[0]["count"], totals[0]["last"]
        await ForumPost.filter(id=post.post_id).update(
            reply_count=max(count - 1, 0), last_activity_at=last or post.thread.created_at)

    # Messages

    async def _write_batch(
        self, plan: _Plan, channel: Channel | None, messages: list,
        post: _PostState | None = None,
    ) -> None:
        await self._apply_batch(plan, channel, messages, post)
        if self.progress is not None:
            await self.progress(self.report.seen, plan.report.name)
        await anyio.sleep(0)

    async def _apply_batch(
        self, plan: _Plan, channel: Channel | None, messages: list,
        post: _PostState | None = None,
    ) -> None:
        report = plan.report
        if not self.options.existing_only:
            self._count_left_out(messages)
        unique = {}
        for message in messages:
            self.author_counts[message.author_id] += 1
            unique.setdefault(message_uuid(self.options.server_id, self.source, message.id), message)
        self.report.seen += len(unique)

        existing = {
            row["uuid"]: row for row in await Message.filter(
                uuid__in=list(unique), server_id=self.options.server_id,
            ).values("uuid", "id", "author_id", "metadata")}
        handovers = []
        fresh = []
        for uid, message in unique.items():
            row = existing.get(uid)
            if row is None:
                fresh.append((uid, message))
                continue
            report.existing_messages += 1
            owner_id, imported = await self._owner(message.author_id)
            if row["author_id"] != owner_id:
                handovers.append((row, owner_id, imported))
        report.handed_over += len(handovers)
        if self.options.existing_only:
            if handovers and not self.dry:
                async with in_transaction():
                    await self._hand_over(handovers)
            return

        opening_source = post.thread.messages[0].id if post and post.thread.messages else None
        prepared = await self._prepare_all(fresh, channel, opening_source)

        report.messages += len(prepared)
        for item in prepared:
            report.attachments += len(item.attachments)
            report.attachment_bytes += sum(a["size"] for a in item.attachments)
        if self.dry:
            return

        await self._resolve_replies(prepared, existing, channel)
        async with in_transaction():
            if post is not None and post.post_id is None:
                await self._create_post(channel, post)
            rows = [self._message_row(item, plan, channel, post) for item in prepared]
            if rows:
                await Message.bulk_create(rows)
            ids = {uid: row["id"] for uid, row in existing.items()}
            ids.update(dict(await Message.filter(
                uuid__in=[item.uuid for item in prepared]).values_list("uuid", "id")))
            await self._create_attachments(prepared, ids, plan, channel)
            await self._hand_over(handovers)
            if post is not None and post.opening_id is None and opening_source is not None:
                opening = ids.get(message_uuid(self.options.server_id, self.source, opening_source))
                if opening is not None:
                    post.opening_id = opening
                    await ForumPost.filter(id=post.post_id).update(opening_message_id=opening)

    def _count_left_out(self, messages: list) -> None:
        for message in messages:
            if message.reactions:
                self.report.left_out.messages_with_reactions += 1
            if message.pinned:
                self.report.left_out.pinned_messages += 1

    async def _prepare_all(
        self, fresh: list, channel: Channel | None, opening_source: str | None,
    ) -> list[_Prepared]:
        prepared = []
        for uid, message in fresh:
            owner_id, imported = await self._owner(message.author_id)
            content = self._content(message)
            if content is None:
                self.report.invalid_skipped += 1
                continue
            embeds = self._embeds(message)
            attachments = await self._attachments(message, channel)
            is_opening = message.id == opening_source
            if not (content["content"] or attachments or embeds or is_opening):
                self.report.empty_skipped += 1
                continue
            reply_uuid = (
                message_uuid(self.options.server_id, self.source, message.reply_to_id)
                if message.reply_to_id else None)
            prepared.append(_Prepared(
                message, uid, owner_id, imported, content, embeds, attachments, reply_uuid))
        return prepared

    def _content(self, message) -> dict | None:
        try:
            return normalize_content(
                to_doc(message.content, self._resolve_mention), allow_empty=True)
        except InvalidContent:
            pass
        try:
            return normalize_content(message.content, allow_empty=True)
        except InvalidContent:
            return None

    @staticmethod
    def _embeds(message) -> list[dict]:
        limits = {"title": 300, "description": 1000, "site_name": 100}
        embeds = []
        for embed in message.embeds:
            data = embed.model_dump()
            for name, limit in limits.items():
                if data[name]:
                    data[name] = data[name][:limit]
            try:
                embeds.append(EmbedIn(**data).model_dump())
            except ValidationError:
                continue
        return embeds[:MAX_EMBEDS_PER_MESSAGE]

    async def _attachments(self, message, channel: Channel | None) -> list[dict]:
        prepared = []
        for attachment in message.attachments:
            label = f"{attachment.filename} (message {message.id})"
            if len(prepared) >= MAX_ATTACHMENTS_PER_MESSAGE:
                self.report.over_attachment_limit += 1
                continue
            path = bundle_format.resolve_file(self.path, attachment.path)
            size = path.stat().st_size if path else 0
            if size == 0:
                declared_over_cap = path is None and attachment.size > max_attachment_bytes()
                self._note("over_cap" if declared_over_cap else "missing", label)
                continue
            if size > max_attachment_bytes():
                self._note("over_cap", label)
                continue
            if self.dry:
                prepared.append({"size": size})
                continue
            data = await anyio.to_thread.run_sync(path.read_bytes)
            sniffed = await anyio.to_thread.run_sync(sniff_image, data)
            kind, width, height = "file", None, None
            content_type = declared_content_type(attachment.content_type)
            if sniffed:
                kind = "image"
                content_type, width, height = sniffed
            prepared.append({
                "filename": sanitize_filename(attachment.filename),
                "content_type": content_type,
                "size": len(data),
                "kind": kind,
                "width": width,
                "height": height,
                "storage_path": await store(data, f"channels/{channel.id}"),
            })
        return prepared

    def _note(self, kind: str, label: str) -> None:
        count = getattr(self.report, kind) + 1
        setattr(self.report, kind, count)
        examples = getattr(self.report, f"{kind}_examples")
        if len(examples) < EXAMPLE_LIMIT:
            examples.append(label)

    async def _resolve_replies(
        self, prepared: list[_Prepared], existing: dict, channel: Channel,
    ) -> None:
        stored = set(existing)
        unknown = {
            item.reply_uuid for item in prepared if item.reply_uuid
        } - stored - {item.uuid for item in prepared}
        if unknown:
            stored |= set(await Message.filter(
                uuid__in=list(unknown), channel_id=channel.id).values_list("uuid", flat=True))
        for item in prepared:
            if item.reply_uuid not in stored:
                item.reply_uuid = None
            stored.add(item.uuid)

    def _message_row(
        self, item: _Prepared, plan: _Plan, channel: Channel, post: _PostState | None,
    ) -> Message:
        metadata = {"import": {"source": self.source, "id": item.source.id}}
        if item.imported_author:
            metadata["imported_author"] = item.imported_author
        if item.embeds:
            metadata["embeds"] = item.embeds
        return Message(
            uuid=item.uuid,
            content=serialize(item.content),
            author_id=item.owner_id,
            server_id=self.options.server_id,
            channel_id=channel.id,
            post_id=post.post_id if post else None,
            created_at=item.source.timestamp,
            timestamp=item.source.timestamp,
            edited_at=item.source.edited_at,
            metadata=metadata,
            reply_to_uuid=item.reply_uuid,
        )

    async def _create_attachments(
        self, prepared: list[_Prepared], ids: dict, plan: _Plan, channel: Channel,
    ) -> None:
        rows = []
        for item in prepared:
            for index, data in enumerate(item.attachments):
                rows.append(Attachment(
                    uploader_id=item.owner_id,
                    server_id=self.options.server_id,
                    channel_id=channel.id,
                    message_id=ids[item.uuid],
                    created_at=item.source.timestamp + timedelta(microseconds=index),
                    **data,
                ))
        if rows:
            await Attachment.bulk_create(rows)

    @staticmethod
    async def _hand_over(handovers: list) -> None:
        for row, owner_id, imported in handovers:
            metadata = dict(row["metadata"] or {})
            if imported:
                metadata["imported_author"] = imported
            else:
                metadata.pop("imported_author", None)
            await Message.filter(id=row["id"]).update(author_id=owner_id, metadata=metadata)
            await Attachment.filter(message_id=row["id"]).update(uploader_id=owner_id)
            for post in await ForumPost.filter(opening_message_id=row["id"]):
                post_metadata = dict(post.metadata or {})
                if imported:
                    post_metadata["imported_author"] = imported
                else:
                    post_metadata.pop("imported_author", None)
                await ForumPost.filter(id=post.id).update(
                    author_id=owner_id, metadata=post_metadata)

    # Unread

    @asynccontextmanager
    async def _unread_guard(self, channel: Channel | None, created: bool):
        """Members who had read everything before the import still have after
        it, even if it stops half way."""
        if self.dry or self.options.existing_only or channel is None:
            yield
            return
        before = await self._top_message(channel.id)
        members = list(await UserToServer.filter(
            server_id=self.options.server_id).values_list("user_id", flat=True))
        if created or before is None:
            caught_up = set(members)
        else:
            caught_up = set()
            for ids in _chunks(members):
                caught_up |= {
                    user_id for user_id, marker in await ReadState.filter(
                        channel_id=channel.id, user_id__in=ids,
                    ).values_list("user_id", "last_read_message_id")
                    if marker >= before}
        try:
            yield
        finally:
            after = await self._top_message(channel.id)
            if after is not None and after != before:
                await self._catch_up(channel.id, caught_up, after)

    @staticmethod
    async def _top_message(channel_id: int) -> int | None:
        rows = await Message.filter(channel_id=channel_id).annotate(
            top=Max("id")).values_list("top", flat=True)
        return rows[0] if rows else None

    @staticmethod
    async def _catch_up(channel_id: int, user_ids: set[int], message_id: int) -> None:
        have = set()
        for ids in _chunks(user_ids):
            await ReadState.filter(
                channel_id=channel_id, user_id__in=ids, last_read_message_id__lt=message_id,
            ).update(last_read_message_id=message_id)
            have |= set(await ReadState.filter(
                channel_id=channel_id, user_id__in=ids).values_list("user_id", flat=True))
        await ReadState.bulk_create([
            ReadState(user_id=user_id, channel_id=channel_id, last_read_message_id=message_id)
            for user_id in user_ids - have])

    # Report

    def _finish(self) -> None:
        report = self.report
        report.free_bytes = shutil.disk_usage(attachments_root()).free
        if not self.options.existing_only:
            report.left_out.unreadable_channels = len(self.server.unreadable)
            report.left_out.emoji = len(self.server.emoji)
            report.left_out.avatars = sum(1 for a in self.server.authors if a.avatar)
        report.authors = sorted(
            (SeenAuthor(author_id, self.names.get(author_id, author_id), count)
             for author_id, count in self.author_counts.items()),
            key=lambda a: (-a.messages, a.name))
        report.unmapped_authors = [
            UnmappedAuthor(a.id, a.name, a.messages)
            for a in report.authors if a.id not in self.users]


async def import_bundle(
    bundle: Path, options: ImportOptions, progress: Progress | None = None,
) -> Report:
    """Plan and, unless options.dry_run, perform the import. *progress* is
    awaited after every batch of messages with the number seen so far and the
    channel's name. Raises BundleError for an unreadable bundle and
    ImportAborted for options that can't be followed."""
    if await Server.get_or_none(id=options.server_id) is None:
        raise ImportAborted(f"No server with id {options.server_id}")
    return await (await anyio.to_thread.run_sync(_Import, bundle, options, progress)).run()


def _size(value: int) -> str:
    size = float(value)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def format_report(report: Report) -> str:
    lines = [f"Bundle {report.source}" + (" (dry run, nothing written)" if report.dry_run else "")]
    lines.append("")
    lines.append("Channels")
    for channel in report.channels:
        how = f" [{channel.matched_by}]" if channel.matched_by else ""
        lines.append(f"  {channel.name} ({channel.type}): {channel.plan}{how}")
        parts = []
        if channel.type == "forum":
            parts.append(f"{channel.posts} posts")
        if channel.type != "voice" and not channel.plan.startswith("skipped"):
            parts.append(f"{channel.messages} messages")
            parts.append(
                f"{channel.attachments} attachments ({_size(channel.attachment_bytes)})")
        if channel.existing_messages:
            parts.append(f"{channel.existing_messages} already imported")
        if channel.handed_over:
            parts.append(f"{channel.handed_over} handed to a mapped user")
        if parts:
            lines.append("    " + ", ".join(parts))
    lines += [
        "",
        f"Total: {report.messages} messages, {report.posts} posts, "
        f"{report.attachments} attachments ({_size(report.attachment_bytes)})",
        f"Free space on the attachments volume: {_size(report.free_bytes)}",
    ]
    lines += _list_section(
        f"Over the size limit ({_size(max_attachment_bytes())}), skipped",
        report.over_cap, report.over_cap_examples)
    lines += _list_section("Missing from the bundle, skipped", report.missing, report.missing_examples)
    left_out = report.left_out
    reasons = [
        (left_out.private_channels, "private channels (use --include-private)"),
        (left_out.unreadable_channels, "channels the exporter couldn't read"),
        (left_out.text_channel_threads, "threads inside text channels"),
        (left_out.messages_with_reactions, "messages with reactions"),
        (left_out.pinned_messages, "pinned messages"),
        (left_out.voice_text_chat, "voice channel text chat messages"),
        (left_out.emoji, "custom emoji"),
        (left_out.avatars, "avatars"),
        (report.over_attachment_limit, "attachments beyond the per-message limit"),
        (report.tags_over_limit, "forum tags beyond the per-channel limit"),
        (report.empty_skipped, "empty messages"),
        (report.invalid_skipped, "messages whose content couldn't be stored"),
    ]
    shown = [f"  {count} {text}" for count, text in reasons if count]
    if shown:
        lines += ["", "Left out", *shown]
    if report.unmapped_authors:
        lines += ["", "Authors with no mapping (owned by the import account)"]
        lines += [f"  {a.name} ({a.id}): {a.messages} messages" for a in report.unmapped_authors]
    if report.warnings:
        lines += ["", "Warnings", *[f"  {w}" for w in report.warnings]]
    return "\n".join(lines)


def _list_section(title: str, count: int, examples: list[str]) -> list[str]:
    if not count:
        return []
    lines = ["", f"{title}: {count}"] + [f"  {e}" for e in examples]
    if count > len(examples):
        lines.append(f"  and {count - len(examples)} more")
    return lines
