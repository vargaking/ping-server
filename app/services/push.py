import ast
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid, _check_sub
from pywebpush import WebPushException, webpush

from app.models.PushSubscription import PushSubscription

logger = logging.getLogger("app.services.push")

PUSH_TTL_SECONDS = 4 * 24 * 60 * 60
DELIVER_TIMEOUT_SECONDS = 10
PREVIEW_MAX_CHARS = 140
MAX_TRACKED_THREADS = 10_000
VAPID_CLAIM_LIFETIME_SECONDS = 12 * 60 * 60
MAX_BODY_CHARS = 500
PAYLOAD_VERSION = 1

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


def endpoint_origin(endpoint: str) -> str:
    """scheme://host[:port] of a push endpoint: the VAPID audience."""
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def endpoint_host(endpoint: str) -> str:
    """Host of a push endpoint. The full URL is a capability, so logs and
    responses only ever carry this."""
    return urlparse(endpoint).hostname or ""


@dataclass(frozen=True)
class DeliveryResult:
    status: int
    body: str = ""


@dataclass(frozen=True)
class SendResult:
    endpoint_host: str
    status: int | None
    error: str | None


# The service worker drops any push that is not JSON with v === 1, a string
# tag and a kind of "dm", "mention" or "read". Every payload is built here.
def dm_tag(conversation_id: int) -> str:
    return f"dm-{conversation_id}"


def channel_tag(channel_id: int) -> str:
    return f"ch-{channel_id}"


def _message_payload(
    kind: str, *, tag: str, title: str, body: str, url: str, count: int,
    message_id: int, message_uuid: str,
) -> dict:
    return {
        "v": PAYLOAD_VERSION,
        "kind": kind,
        "tag": tag,
        "title": title,
        "body": body,
        "url": url,
        "count": count,
        "message_id": message_id,
        "message_uuid": message_uuid,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def dm_payload(
    *, conversation_id: int, sender_name: str, body: str, count: int,
    message_id: int, message_uuid: str,
) -> dict:
    return _message_payload(
        "dm", tag=dm_tag(conversation_id), title=sender_name, body=body,
        url=f"/app/direct/{conversation_id}/", count=count,
        message_id=message_id, message_uuid=message_uuid)


def mention_payload(
    *, server_id: int, channel_id: int, channel_name: str, sender_name: str,
    body: str, message_id: int, message_uuid: str,
) -> dict:
    return _message_payload(
        "mention", tag=channel_tag(channel_id), title=f"{sender_name} in #{channel_name}",
        body=body, url=f"/app/server/{server_id}/channel/{channel_id}/", count=1,
        message_id=message_id, message_uuid=message_uuid)


def read_payload(tag: str) -> dict:
    return {"v": PAYLOAD_VERSION, "kind": "read", "tag": tag}


def test_payload() -> dict:
    """A "dm" payload, so service workers already deployed show it."""
    return _message_payload(
        "dm", tag="test", title="Test notification",
        body="Push notifications are working.", url="/app/", count=1,
        message_id=0, message_uuid=str(uuid.uuid4()))


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


def normalize_subject(value: str) -> str:
    """A VAPID subject in the one shape push services accept: https URLs lose
    their path, query, userinfo and port; mailto: and anything else is kept."""
    value = value.strip()
    if not value.lower().startswith("https://"):
        return value
    try:
        host = urlparse(value).hostname
    except ValueError:
        return value
    return f"https://{host}" if host else value


def is_valid_subject(value: str) -> bool:
    return bool(_check_sub(value))


def default_subject() -> str | None:
    """The app's public URL: the first https origin in ALLOWED_ORIGINS."""
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip()
        if origin.lower().startswith("https://"):
            return origin
    return None


class PushService:
    """Sends Web Push messages to a user's subscribed browsers."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # (user_id, tag) -> pk of the last message we pushed for that thread.
        # In memory and single-process, like the permission cache; a restart
        # only means an outstanding notification isn't closed by a read.
        self._pushed: dict[tuple[int, str], int] = {}
        self.configure(log=False)

    def configure(self, *, log: bool = True) -> None:
        """(Re)read the VAPID settings from the environment. Push stays
        disabled unless both keys are usable and the subject is valid."""
        self._vapid: Vapid | None = None
        self.public_key: str | None = None
        self.subject: str | None = None
        self.disabled_reason: str | None = None

        public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
        private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
        if not (public_key and private_key):
            self.disabled_reason = "VAPID keys not set"
            return

        def disable(reason: str, message: str, *args) -> None:
            self.disabled_reason = reason
            if log:
                logger.error(message, *args)

        try:
            vapid = load_private_key(private_key)
        except Exception:
            disable("invalid VAPID keys", "VAPID_PRIVATE_KEY is not a usable key; push disabled")
            return
        if public_key_b64(vapid) != public_key.rstrip("="):
            disable(
                "invalid VAPID keys",
                "VAPID_PUBLIC_KEY does not match VAPID_PRIVATE_KEY; push disabled")
            return

        configured = os.getenv("VAPID_SUBJECT", "").strip()
        subject = normalize_subject(configured or default_subject() or "")
        if not is_valid_subject(subject):
            if configured:
                disable(
                    "invalid VAPID subject",
                    "VAPID_SUBJECT is missing or invalid (%r); it must be mailto:<address> "
                    "or https://<host>. Push disabled", configured)
            else:
                disable(
                    "invalid VAPID subject",
                    "VAPID_SUBJECT is unset and ALLOWED_ORIGINS has no https origin; it must "
                    "be mailto:<address> or https://<host>. Push disabled")
            return
        self._vapid = vapid
        self.public_key = public_key
        self.subject = subject
        if log:
            logger.info("Push enabled; VAPID subject %s", self.subject)

    @property
    def enabled(self) -> bool:
        return self._vapid is not None

    async def deliver(
        self, sub: PushSubscription, payload: dict, *, topic: str, urgency: str,
    ) -> DeliveryResult:
        """Encrypt and POST *payload* to one subscription; returns the push
        service's HTTP status and body. The only place that talks to the network."""
        return await asyncio.to_thread(self._deliver_blocking, sub, payload, topic, urgency)

    def _deliver_blocking(
        self, sub: PushSubscription, payload: dict, topic: str, urgency: str,
    ) -> DeliveryResult:
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
                vapid_claims={
                    "sub": self.subject,
                    "aud": endpoint_origin(sub.endpoint),
                    "exp": int(time.time()) + VAPID_CLAIM_LIFETIME_SECONDS,
                },
                content_encoding="aes128gcm",
                ttl=PUSH_TTL_SECONDS,
                headers={"Topic": topic, "Urgency": urgency},
                timeout=DELIVER_TIMEOUT_SECONDS,
                requests_session=session,
            )
            return DeliveryResult(response.status_code, response.text[:MAX_BODY_CHARS])
        except WebPushException as exc:
            if exc.response is not None:
                return DeliveryResult(exc.response.status_code, exc.response.text[:MAX_BODY_CHARS])
            raise
        finally:
            session.close()

    async def send_to_user(self, user_id: int, payload: dict, *, topic: str, urgency: str) -> None:
        """Push *payload* to every subscription of *user_id*. Gone
        subscriptions (404/410) are deleted; other failures are only logged."""
        subs = await PushSubscription.filter(user_id=user_id)
        if not subs:
            logger.info("Push skipped for user %s: no subscription", user_id)
            return
        await asyncio.gather(*(
            self._send_one(sub, payload, topic, urgency) for sub in subs))

    async def send_test(self, user_id: int) -> list[SendResult]:
        """Send a test push to every subscription of *user_id* right now and
        report what each push service answered."""
        subs = await PushSubscription.filter(user_id=user_id)
        payload = test_payload()
        return list(await asyncio.gather(*(
            self._send_one(sub, payload, "test", "high") for sub in subs)))

    async def _send_one(
        self, sub: PushSubscription, payload: dict, topic: str, urgency: str,
    ) -> SendResult:
        host = endpoint_host(sub.endpoint)
        try:
            result = await self.deliver(sub, payload, topic=topic, urgency=urgency)
        except Exception as exc:
            logger.warning(
                "Push to user %s via %s raised", sub.user_id, host, exc_info=True)
            return SendResult(host, None, f"{type(exc).__name__}: {exc}")

        status = result.status
        if 200 <= status < 300:
            logger.info("Push sent to user %s via %s: %s", sub.user_id, host, status)
            await PushSubscription.filter(id=sub.id).update(
                last_used_at=datetime.now(timezone.utc))
            return SendResult(host, status, None)

        logger.warning(
            "Push to user %s via %s failed: %s %s", sub.user_id, host, status, result.body)
        if status in (404, 410):
            logger.info("Subscription of user %s via %s is gone; removed", sub.user_id, host)
            await PushSubscription.filter(id=sub.id).delete()
        return SendResult(host, status, result.body or f"HTTP {status}")

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
            user_id, read_payload(tag), topic=tag, urgency="normal"))


push = PushService()
