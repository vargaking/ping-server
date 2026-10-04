"""Forum channels: post rows for the index and frames, tag helpers and the
index queries. A post's messages are ordinary messages (see ChatService)."""
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from tortoise.expressions import Q, Subquery
from tortoise.functions import Count

from ..models.ForumPost import ForumPost
from ..models.ForumPostTag import ForumPostTag
from ..models.ForumTag import ForumTag
from ..models.Message import Message
from .attachments import attachments_by_message

MAX_TAGS_PER_CHANNEL = 20
MAX_TAGS_PER_POST = 5
TITLE_MAX = 200
TAG_NAME_MAX = 30
POST_PAGE_SIZE = 200


@dataclass
class NewPost:
    title: str
    tag_ids: list[int]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def check_tag_ids(channel_id: int, tag_ids: list[int]) -> list[int]:
    """The ids without repeats, or 422 if there are too many or one isn't a
    tag of this channel."""
    unique = list(dict.fromkeys(tag_ids))
    if len(unique) > MAX_TAGS_PER_POST:
        raise HTTPException(
            status_code=422, detail=f"A post can have at most {MAX_TAGS_PER_POST} tags")
    if unique and await ForumTag.filter(
            channel_id=channel_id, id__in=unique).count() != len(unique):
        raise HTTPException(status_code=422, detail="Unknown tag for this channel")
    return unique


async def set_post_tags(post_id: int, tag_ids: list[int]) -> None:
    await ForumPostTag.filter(post_id=post_id).delete()
    await ForumPostTag.bulk_create(
        [ForumPostTag(post_id=post_id, tag_id=tag_id) for tag_id in tag_ids])


async def create_post(channel_id: int, author_id: int, new_post: NewPost) -> ForumPost:
    """Insert the post and its tags. The caller links the opening message
    inside the same transaction."""
    post = await ForumPost.create(
        channel_id=channel_id, author_id=author_id, title=new_post.title,
        last_activity_at=utcnow())
    await set_post_tags(post.id, new_post.tag_ids)
    return post


def tag_json(tag: ForumTag) -> dict:
    return {
        "id": tag.id,
        "channel_id": tag.channel_id,
        "name": tag.name,
        "color": tag.color,
        "position": tag.position,
    }


async def channel_tags(channel_id: int) -> list[dict]:
    tags = await ForumTag.filter(channel_id=channel_id).order_by("position", "id")
    return [tag_json(tag) for tag in tags]


def _thumbnail(metadata: dict | None, attachments: list[dict]) -> dict | None:
    for attachment in attachments:
        if attachment["kind"] == "image":
            return {"type": "image", "attachment": attachment}
    for embed in (metadata or {}).get("embeds", []):
        if embed.get("image_url"):
            return {"type": "embed", "url": embed["image_url"]}
    for attachment in attachments:
        if attachment["content_type"].startswith("video/"):
            return {"type": "video", "attachment": attachment}
    return None


async def rows(posts: list[ForumPost], *, with_opening_id: bool = False) -> list[dict]:
    """Wire form of each post: two queries for the thumbnails and one for the
    tags, however many posts there are."""
    if not posts:
        return []
    post_ids = [post.id for post in posts]
    tag_ids: dict[int, list[int]] = {}
    for post_id, tag_id in await ForumPostTag.filter(post_id__in=post_ids).order_by(
            "tag__position", "tag_id").values_list("post_id", "tag_id"):
        tag_ids.setdefault(post_id, []).append(tag_id)

    opening_ids = [p.opening_message_id for p in posts if p.opening_message_id]
    openings = {
        m["id"]: m for m in await Message.filter(id__in=opening_ids).values(
            "id", "uuid", "metadata")}
    attachments = await attachments_by_message(opening_ids)

    result = []
    for post in posts:
        opening = openings.get(post.opening_message_id)
        row = {
            "id": post.id,
            "channel_id": post.channel_id,
            "title": post.title,
            "author_id": post.author_id,
            "tag_ids": tag_ids.get(post.id, []),
            "pinned": post.pinned,
            "locked": post.locked,
            "reply_count": post.reply_count,
            "created_at": post.created_at.isoformat(),
            "last_activity_at": post.last_activity_at.isoformat(),
            "thumbnail": opening and _thumbnail(
                opening["metadata"], attachments.get(opening["id"], [])),
        }
        if with_opening_id:
            row["opening_message_id"] = opening and str(opening["uuid"])
        result.append(row)
    return result


def _matching_all(tag_ids: list[int]) -> Subquery:
    return Subquery(
        ForumPostTag.filter(tag_id__in=tag_ids)
        .annotate(matched=Count("id"))
        .group_by("post_id")
        .filter(matched=len(tag_ids))
        .values("post_id"))


async def index_page(
    channel_id: int, tag_ids: list[int], cursor: tuple[datetime, int] | None,
) -> tuple[list[ForumPost], bool]:
    """Unpinned posts by (last_activity_at, id) descending, one page after
    *cursor*, and whether more follow. The first page (no cursor) is preceded
    by every pinned post."""
    base = ForumPost.filter(channel_id=channel_id)
    if tag_ids:
        base = base.filter(id__in=_matching_all(tag_ids))

    pinned: list[ForumPost] = []
    if cursor is None:
        pinned = await base.filter(pinned=True).order_by("-last_activity_at", "-id")

    query = base.filter(pinned=False)
    if cursor is not None:
        at, post_id = cursor
        query = query.filter(
            Q(last_activity_at__lt=at) | Q(last_activity_at=at, id__lt=post_id))
    page = await query.order_by("-last_activity_at", "-id").limit(POST_PAGE_SIZE + 1)
    return pinned + page[:POST_PAGE_SIZE], len(page) > POST_PAGE_SIZE


def created_frame(server_id: int, row: dict) -> dict:
    return {
        "type": "forum_post_created",
        "server_id": server_id,
        "channel_id": row["channel_id"],
        "post": row,
    }


def updated_frame(server_id: int, row: dict) -> dict:
    return {**created_frame(server_id, row), "type": "forum_post_updated"}


def deleted_frame(server_id: int, channel_id: int, post_id: int) -> dict:
    return {
        "type": "forum_post_deleted",
        "server_id": server_id,
        "channel_id": channel_id,
        "post_id": post_id,
    }


def tags_frame(server_id: int, channel_id: int, tags: list[dict]) -> dict:
    return {
        "type": "forum_tags_updated",
        "server_id": server_id,
        "channel_id": channel_id,
        "tags": tags,
    }
