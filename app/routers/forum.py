import re
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, StrictInt, field_validator
from tortoise.exceptions import IntegrityError
from tortoise.functions import Max
from tortoise.transactions import in_transaction

from ..middleware import get_current_user
from ..models.Attachment import Attachment
from ..models.Channel import Channel
from ..models.ForumPost import ForumPost
from ..models.ForumTag import ForumTag
from ..models.Message import Message
from ..models.Server import Server
from ..models.User import User
from ..permissions import (
    Permission,
    channel_from_path,
    check_permission,
    require_permission,
    server_of_channel,
)
from ..services import channel_layout, forum
from ..services.attachments import MAX_ATTACHMENTS_PER_MESSAGE, delete_file
from ..services.chat_service import MessageAlreadyStored, MessageRejected
from ..services.permissions import permissions
from ..ws_schemas import MAX_EMBEDS_PER_MESSAGE, EmbedIn, MessageFrame
from .channels import _decode_cursor, _encode_cursor

router = APIRouter(tags=["forum"])

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _clean_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Title can't be empty")
    if len(value) > forum.TITLE_MAX:
        raise ValueError(f"Title must be at most {forum.TITLE_MAX} characters")
    return value


def _clean_tag_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Tag name can't be empty")
    if len(value) > forum.TAG_NAME_MAX:
        raise ValueError(f"Tag name must be at most {forum.TAG_NAME_MAX} characters")
    return value


def _clean_color(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not _COLOR.fullmatch(value):
        raise ValueError("Color must look like #rrggbb")
    return value.lower()


class OpeningMessage(BaseModel):
    id: UUID
    # A ProseMirror doc (dict) or plain string; validated by normalize_content.
    content: Any
    timestamp: str
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)
    embeds: list[EmbedIn] = Field(default_factory=list, max_length=MAX_EMBEDS_PER_MESSAGE)

    @field_validator("timestamp")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class PostCreate(BaseModel):
    title: str
    tag_ids: list[StrictInt] = Field(default_factory=list, max_length=100)
    message: OpeningMessage

    _title = field_validator("title")(_clean_title)


class PostUpdate(BaseModel):
    title: Optional[str] = None
    tag_ids: Optional[list[StrictInt]] = Field(default=None, max_length=100)
    pinned: Optional[bool] = None
    locked: Optional[bool] = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_title(value)


class TagCreate(BaseModel):
    name: str
    color: Optional[str] = None

    _name = field_validator("name")(_clean_tag_name)
    _color = field_validator("color")(_clean_color)


class TagUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_tag_name(value)

    _color = field_validator("color")(_clean_color)


class TagOrder(BaseModel):
    tag_ids: list[StrictInt] = Field(max_length=forum.MAX_TAGS_PER_CHANNEL)


async def _forum_channel(channel: Channel = Depends(channel_from_path)) -> Channel:
    if channel.type != "forum":
        raise HTTPException(status_code=422, detail="Not a forum channel")
    return channel


async def _broadcast(request: Request, server_id: int, frame: dict) -> None:
    comms = getattr(request.app.state, "comms", None)
    if comms is not None:
        await comms.broadcast_to_server(server_id, frame)


async def _post_with_server(post_id: int, user: User) -> tuple[ForumPost, Server]:
    """A post of a server the caller can view; 404 for an unknown post."""
    post = await ForumPost.get_or_none(id=post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    channel = await Channel.get(id=post.channel_id)
    server = await Server.get(id=channel.server_id)
    await require_permission(user, server, Permission.VIEW_CHANNEL)
    return post, server


async def _channel_post(channel_id: int, post_id: int) -> ForumPost:
    post = await ForumPost.get_or_none(id=post_id, channel_id=channel_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.get("/channels/{channel_id}/posts")
async def list_posts(
    cursor: Optional[str] = None,
    tag_ids: List[int] = Query(default_factory=list),
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    """Pinned posts (first page only), then the rest by latest activity.
    With tag_ids, only posts that carry every one of those tags."""
    tag_ids = await forum.check_tag_ids(channel.id, tag_ids)
    posts, has_more = await forum.index_page(
        channel.id, tag_ids, _decode_cursor(cursor) if cursor else None)
    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor(posts[-1].last_activity_at, posts[-1].id)
    return {"posts": await forum.rows(posts), "next_cursor": next_cursor}


@router.get("/channels/{channel_id}/posts/{post_id}")
async def get_post(
    post_id: int,
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    post = await _channel_post(channel.id, post_id)
    return (await forum.rows([post], with_opening_id=True))[0]


_REJECTED_STATUS = {"forbidden": 403, "duplicate_id": 409}


@router.post("/channels/{channel_id}/posts", status_code=status.HTTP_201_CREATED)
async def create_post(
    body: PostCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    server: Server = Depends(check_permission(
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    """A post and its opening message, created together through the same
    pipeline as every other message."""
    tag_ids = await forum.check_tag_ids(channel.id, body.tag_ids)
    opening = body.message
    frame = MessageFrame(
        type="message", id=str(opening.id), server_id=server.id, channel_id=channel.id,
        content=opening.content, timestamp=opening.timestamp,
        attachment_ids=opening.attachment_ids, embeds=opening.embeds)
    chat = request.app.state.comms.chat_service
    try:
        created = await chat.create_channel_message(
            current_user.id, frame, post=forum.NewPost(body.title, tag_ids),
            skip_sender=True)
    except MessageAlreadyStored:
        raise HTTPException(status_code=409, detail="duplicate_id")
    except MessageRejected as rejected:
        raise HTTPException(
            status_code=_REJECTED_STATUS.get(rejected.code, 422), detail=rejected.code)
    return {"post": created.post_row, "message": created.frame}


@router.patch("/posts/{post_id}")
async def update_post(
    post_id: int,
    body: PostUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Title and tags: the author or MANAGE_MESSAGES. Pin and lock:
    MANAGE_MESSAGES."""
    post, server = await _post_with_server(post_id, current_user)
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Fields can't be null")

    moderator = await permissions.has(current_user.id, server, Permission.MANAGE_MESSAGES)
    if not moderator and (
            "pinned" in changes or "locked" in changes
            or post.author_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not allowed to edit this post")

    tag_ids = None
    if "tag_ids" in changes:
        tag_ids = await forum.check_tag_ids(post.channel_id, changes.pop("tag_ids"))

    changes = {k: v for k, v in changes.items() if getattr(post, k) != v}
    async with in_transaction():
        if changes:
            await post.update_from_dict(changes)
            await post.save(update_fields=list(changes))
        if tag_ids is not None:
            await forum.set_post_tags(post.id, tag_ids)

    row = (await forum.rows([post]))[0]
    if changes or tag_ids is not None:
        await _broadcast(request, server.id, forum.updated_frame(server.id, row))
    return row


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Delete a post with its messages and their files. The author or
    MANAGE_MESSAGES."""
    post, server = await _post_with_server(post_id, current_user)
    if post.author_id != current_user.id and not await permissions.has(
            current_user.id, server, Permission.MANAGE_MESSAGES):
        raise HTTPException(status_code=403, detail="Not allowed to delete this post")

    message_ids = await Message.filter(post_id=post.id).values_list("id", flat=True)
    storage_paths = await Attachment.filter(
        message_id__in=message_ids).values_list("storage_path", flat=True)
    async with in_transaction():
        await Message.filter(post_id=post.id).delete()
        await post.delete()
    for storage_path in storage_paths:
        delete_file(storage_path)

    await _broadcast(
        request, server.id, forum.deleted_frame(server.id, post.channel_id, post.id))


@router.get("/channels/{channel_id}/tags")
async def list_tags(
    server: Server = Depends(check_permission(Permission.VIEW_CHANNEL, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    return await forum.channel_tags(channel.id)


async def _tags_changed(request: Request, server: Server, channel: Channel) -> list[dict]:
    tags = await forum.channel_tags(channel.id)
    await _broadcast(request, server.id, forum.tags_frame(server.id, channel.id, tags))
    return tags


async def _channel_tag(channel_id: int, tag_id: int) -> ForumTag:
    tag = await ForumTag.get_or_none(id=tag_id, channel_id=channel_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


async def _name_taken(channel_id: int, name: str, *, except_id: int | None = None) -> bool:
    query = ForumTag.filter(channel_id=channel_id, name__iexact=name)
    if except_id is not None:
        query = query.exclude(id=except_id)
    return await query.exists()


_NAME_TAKEN = "A tag with this name already exists"


@router.post(
    "/channels/{channel_id}/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(
    body: TagCreate,
    request: Request,
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    try:
        async with channel_layout.locked_server(server.id):
            if await ForumTag.filter(channel_id=channel.id).count() >= forum.MAX_TAGS_PER_CHANNEL:
                raise HTTPException(
                    status_code=422,
                    detail=f"A forum can have at most {forum.MAX_TAGS_PER_CHANNEL} tags")
            if await _name_taken(channel.id, body.name):
                raise HTTPException(status_code=409, detail=_NAME_TAKEN)
            last = await ForumTag.filter(channel_id=channel.id).annotate(
                last=Max("position")).values_list("last", flat=True)
            tag = await ForumTag.create(
                channel_id=channel.id, name=body.name, color=body.color,
                position=(last[0] + 1) if last and last[0] is not None else 0)
    except IntegrityError:
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    await _tags_changed(request, server, channel)
    return forum.tag_json(tag)


@router.put("/channels/{channel_id}/tags/order")
async def reorder_tags(
    body: TagOrder,
    request: Request,
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    """Set the tag order. *tag_ids* must be exactly the channel's tags."""
    async with channel_layout.locked_server(server.id):
        existing = await ForumTag.filter(channel_id=channel.id).values_list("id", flat=True)
        if sorted(body.tag_ids) != sorted(existing):
            raise HTTPException(
                status_code=422, detail="tag_ids must list every tag of the channel once")
        for position, tag_id in enumerate(body.tag_ids):
            await ForumTag.filter(id=tag_id).update(position=position)
    return await _tags_changed(request, server, channel)


@router.patch("/channels/{channel_id}/tags/{tag_id}")
async def update_tag(
    tag_id: int,
    body: TagUpdate,
    request: Request,
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Tag name can't be empty")
    tag = await _channel_tag(channel.id, tag_id)
    if "name" in changes and await _name_taken(channel.id, changes["name"], except_id=tag.id):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    if changes:
        try:
            await tag.update_from_dict(changes)
            await tag.save(update_fields=list(changes))
        except IntegrityError:
            raise HTTPException(status_code=409, detail=_NAME_TAKEN)
        await _tags_changed(request, server, channel)
    return forum.tag_json(tag)


@router.delete(
    "/channels/{channel_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: int,
    request: Request,
    server: Server = Depends(check_permission(Permission.MANAGE_CHANNELS, server_of_channel)),
    channel: Channel = Depends(_forum_channel),
):
    tag = await _channel_tag(channel.id, tag_id)
    await tag.delete()
    await _tags_changed(request, server, channel)
