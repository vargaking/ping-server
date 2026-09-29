import ast
import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from app.models.PushSubscription import PushSubscription

logger = logging.getLogger("app.services.push")

PUSH_TTL_SECONDS = 4 * 24 * 60 * 60
DELIVER_TIMEOUT_SECONDS = 10
PREVIEW_MAX_CHARS = 140
MAX_TRACKED_THREADS = 10_000

# Push services the browsers use. The server POSTs to whatever endpoint a
# client registers, so anything else is refused. Matched by host suffix.
PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
    "web.push.apple.com",
)


def is_allowed_endpoint(endpoint: str) -> bool:
    """Whether *endpoint* is an https URL on a known push service (or on a host
    listed in PUSH_EXTRA_HOSTS)."""
    try:
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    extra = tuple(
        h.strip().lower() for h in os.getenv("PUSH_EXTRA_HOSTS", "").split(",") if h.strip())
    return any(host == s or host.endswith(f".{s}") for s in PUSH_HOST_SUFFIXES + extra)


# Message content is client-controlled: bound how much of it we read.
MAX_LITERAL_EVAL_CHARS = 20_000
MAX_CONTENT_NODES = 10_000
MAX_CONTENT_DEPTH = 100


def _parse_content(raw):
    """Message content as stored or sent: a tiptap dict, a JSON string, a
    Python-style stringified dict, or legacy plain text."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        pass
    if len(raw) <= MAX_LITERAL_EVAL_CHARS:
        try:
            return ast.literal_eval(raw)
        except Exception:
            pass
    return raw


def _walk(root):
    """Yield (node, entering) for every dict node under *root*, depth first:
    True on the way in, False after its children. Iterative and bounded, so
    hostile nesting can't exhaust the stack or run for long."""
    stack = [(root, 0, True)]
    seen = 0
    while stack:
        node, depth, entering = stack.pop()
        if not entering:
            yield node, False
            continue
        if not isinstance(node, dict) or seen >= MAX_CONTENT_NODES:
            continue
        seen += 1
        yield node, True
        stack.append((node, depth, False))
        children = node.get("content")
        if isinstance(children, list) and depth < MAX_CONTENT_DEPTH:
            stack.extend((child, depth + 1, True) for child in reversed(children))


def plain_text(content, attachments=()) -> str:
    """One-line preview of a message, at most PREVIEW_MAX_CHARS characters.

    Ported from messagePlainText / messagePreviewText in the frontend.
    """
    parsed = _parse_content(content)
    if isinstance(parsed, dict):
        parts: list[str] = []
        for node, entering in _walk(parsed):
            node_type = node.get("type")
            if not entering:
                # Separate block nodes (paragraphs, list items) with a space.
                if isinstance(node.get("content"), list) and node_type != "doc":
                    parts.append(" ")
                continue
            attrs = node.get("attrs")
            attrs = attrs if isinstance(attrs, dict) else {}
            if node_type == "text":
                parts.append(str(node.get("text") or ""))
            elif node_type == "mention":
                parts.append(f"@{attrs.get('label') or attrs.get('id') or ''}")
            elif node_type == "hardBreak":
                parts.append(" ")
        text = " ".join("".join(parts).split())
    elif parsed is None:
        text = ""
    else:
        text = " ".join(str(parsed).split())

    if not text and attachments:
        if len(attachments) > 1:
            text = f"Sent {len(attachments)} attachments"
        else:
            text = "Sent an image" if attachments[0].kind == "image" else "Sent a file"
    if len(text) > PREVIEW_MAX_CHARS:
        text = text[:PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def mentioned_user_ids(content) -> set[int]:
    """User ids of every mention node in a tiptap document."""
    found: set[int] = set()
    for node, entering in _walk(_parse_content(content)):
        if not entering or node.get("type") != "mention":
            continue
        attrs = node.get("attrs")
        raw = attrs.get("id") if isinstance(attrs, dict) else None
        if isinstance(raw, (int, str)) and not isinstance(raw, bool):
            try:
                found.add(int(raw))
            except ValueError:
                pass
    return found


def public_key_b64(vapid: Vapid) -> str:
    """The applicationServerKey: the uncompressed P-256 point, base64url."""
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_private_key(value: str) -> Vapid:
    """A VAPID private key as a base64url raw 32-byte key or a PEM."""
    value = value.strip().replace("\\n", "\n")
    if value.startswith("-----BEGIN"):
        return Vapid.from_pem(value.encode())
    return Vapid.from_string(value)


class PushService:
    """Sends Web Push messages to a user's subscribed browsers."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # (user_id, tag) -> pk of the last message we pushed for that thread.
        # In memory and single-process, like the permission cache; a restart
        # only means an outstanding notification isn't closed by a read.
        self._pushed: dict[tuple[int, str], int] = {}
        self.configure()

    def configure(self) -> None:
        """(Re)read the VAPID settings from the environment. Push stays
        disabled unless all three are set and usable."""
        self._vapid: Vapid | None = None
        self.public_key: str | None = None
        self.subject: str | None = None

        public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
        private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
        subject = os.getenv("VAPID_SUBJECT", "").strip()
        if not (public_key and private_key and subject):
            return
        try:
            vapid = load_private_key(private_key)
        except Exception:
            logger.error("VAPID_PRIVATE_KEY is not a usable key; push disabled")
            return
        if public_key_b64(vapid) != public_key.rstrip("="):
            logger.error("VAPID_PUBLIC_KEY does not match VAPID_PRIVATE_KEY; push disabled")
            return
        if not subject.startswith(("mailto:", "https://")):
            logger.error("VAPID_SUBJECT must be a mailto: or https:// URL; push disabled")
            return
        self._vapid = vapid
        self.public_key = public_key
        self.subject = subject

    @property
    def enabled(self) -> bool:
        return self._vapid is not None

    async def deliver(self, sub: PushSubscription, payload: dict, *, topic: str, urgency: str) -> int:
        """Encrypt and POST *payload* to one subscription; returns the push
        service's HTTP status. The only place that talks to the network."""
        return await asyncio.to_thread(self._deliver_blocking, sub, payload, topic, urgency)

    def _deliver_blocking(self, sub: PushSubscription, payload: dict, topic: str, urgency: str) -> int:
        session = requests.Session()
        # The endpoint was allowlisted; never follow it somewhere else.
        session.max_redirects = 0
        try:
            response = webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self.subject},
                ttl=PUSH_TTL_SECONDS,
                headers={"Topic": topic, "Urgency": urgency},
                timeout=DELIVER_TIMEOUT_SECONDS,
                requests_session=session,
            )
            return response.status_code
        except WebPushException as exc:
            if exc.response is not None:
                return exc.response.status_code
            raise
        finally:
            session.close()

    async def send_to_user(self, user_id: int, payload: dict, *, topic: str, urgency: str) -> None:
        """Push *payload* to every subscription of *user_id*. Gone
        subscriptions (404/410) are deleted; other failures are only logged."""
        subs = await PushSubscription.filter(user_id=user_id)
        await asyncio.gather(*(
            self._send_one(sub, payload, topic, urgency) for sub in subs))

    async def _send_one(self, sub: PushSubscription, payload: dict, topic: str, urgency: str) -> None:
        try:
            status = await self.deliver(sub, payload, topic=topic, urgency=urgency)
            if status in (404, 410):
                await PushSubscription.filter(id=sub.id).delete()
            elif 200 <= status < 300:
                await PushSubscription.filter(id=sub.id).update(
                    last_used_at=datetime.now(timezone.utc))
            else:
                logger.warning(
                    "Push to subscription %s (user %s) failed with status %s",
                    sub.id, sub.user_id, status)
        except Exception:
            logger.warning(
                "Push to subscription %s (user %s) failed", sub.id, sub.user_id, exc_info=True)

    def schedule(self, coro) -> None:
        """Run *coro* in the background. The sender's socket handler must
        never wait on the push services."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Background push failed", exc_info=task.exception())

    async def drain(self) -> None:
        """Wait for every scheduled push to finish (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def track(self, user_id: int, tag: str, message_pk: int) -> None:
        """Remember that *user_id* was pushed about *message_pk* in *tag*'s thread."""
        key = (user_id, tag)
        self._pushed.pop(key, None)
        self._pushed[key] = message_pk
        if len(self._pushed) > MAX_TRACKED_THREADS:
            del self._pushed[next(iter(self._pushed))]

    def notify_read(self, user_id: int, tag: str, marker: int) -> None:
        """A read marker moved to *marker*. If it covers a message we pushed,
        tell the user's browsers to drop that thread's notification."""
        pushed = self._pushed.get((user_id, tag))
        if pushed is None or marker < pushed:
            return
        del self._pushed[(user_id, tag)]
        if not self.enabled:
            return
        self.schedule(self.send_to_user(
            user_id, {"v": 1, "kind": "read", "tag": tag}, topic=tag, urgency="normal"))



push = PushService()
