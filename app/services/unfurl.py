"""Fetch a page and extract link-preview metadata, without being usable as an
SSRF proxy.

The server is the one making the request, so every address it connects to must
be public. The check happens in the network backend at connect time, on the
addresses the connection will actually use, so a hostname that resolves to a
private address (or flips between lookups) never gets a socket.
"""
import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import anyio
import httpcore
import httpx

from app.ws_schemas import MAX_URL_LENGTH, EmbedIn

logger = logging.getLogger("app.services.unfurl")

MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 3
MAX_BODY_BYTES = 512 * 1024
ALLOWED_PORTS = (None, 80, 443)

CACHE_SIZE = 1000
HIT_TTL_SECONDS = 24 * 60 * 60
MISS_TTL_SECONDS = 10 * 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZetaLinkPreview/1.0)",
    "Accept": "text/html",
    "Accept-Language": "en",
}

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class BlockedURL(ValueError):
    """The URL, or an address it leads to, is not something we may fetch."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return True
    if ip.ipv4_mapped is not None:
        return _is_public(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        return _is_public(ip.sixtofour)
    if ip.teredo is not None:
        return all(_is_public(part) for part in ip.teredo)
    if ip in _NAT64_PREFIX:
        return _is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    # Deprecated IPv4-compatible form (::a.b.c.d), which is_global lets through.
    return int(ip) >> 32 != 0


async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]


_tcp_backend = httpcore.AnyIOBackend


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, refuses non-public answers and connects to the
    address it checked. TLS and the Host header still use the hostname, because
    httpcore only hands us the host to dial, not the one it verifies."""

    def __init__(self) -> None:
        self._inner = _tcp_backend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await _resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        if not addresses or not all(_is_public(a) for a in addresses):
            raise BlockedURL("host resolves to a non-public address")
        return await self._inner.connect_tcp(
            str(addresses[0]), port, timeout=timeout,
            local_address=local_address, socket_options=socket_options)

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedTransport(httpx.AsyncBaseTransport):
    """httpx transport on an httpcore pool using the public-only backend."""

    def __init__(self) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=1,
            network_backend=_PublicOnlyBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = await self._pool.handle_async_request(core_request)
        except (httpcore.ConnectionNotAvailable, httpcore.ProxyError, httpcore.UnsupportedProtocol,
                httpcore.ProtocolError, httpcore.TimeoutException, httpcore.NetworkError) as exc:
            raise httpx.TransportError(str(exc)) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _check_url(raw: str) -> httpx.URL:
    if len(raw) > MAX_URL_LENGTH:
        raise BlockedURL("URL too long")
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise BlockedURL("malformed URL") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise BlockedURL("only http(s) URLs with a host are allowed")
    if url.userinfo:
        raise BlockedURL("credentials in URL")
    if url.port not in ALLOWED_PORTS:
        raise BlockedURL("port not allowed")
    try:
        literal = ipaddress.ip_address(url.host)
    except ValueError:
        return url
    if not _is_public(literal):
        raise BlockedURL("non-public address")
    return url


async def _fetch_html(url: httpx.URL) -> tuple[str, httpx.URL] | None:
    """Body text and final URL of an HTML page, or None if there isn't one."""
    async with httpx.AsyncClient(
        transport=_PinnedTransport(),
        trust_env=False,
        follow_redirects=False,
        headers=HEADERS,
        timeout=TIMEOUT_SECONDS,
    ) as http:
        for _ in range(MAX_REDIRECTS + 1):
            async with http.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    try:
                        target = str(url.join(location))
                    except httpx.InvalidURL as exc:
                        raise BlockedURL("malformed redirect") from exc
                    url = _check_url(target)
                    continue
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200 or not content_type.lower().startswith("text/html"):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= MAX_BODY_BYTES:
                        break
                encoding = response.charset_encoding or "utf-8"
                try:
                    text = bytes(body[:MAX_BODY_BYTES]).decode(encoding, errors="replace")
                except LookupError:
                    text = bytes(body[:MAX_BODY_BYTES]).decode("utf-8", errors="replace")
                return text, url
    return None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attributes = dict(attrs)
            key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
            content = attributes.get("content")
            if key and content is not None:
                self.meta.setdefault(key, content)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and not self.done:
            self.title += data


_WHITESPACE = re.compile(r"\s+")


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = _WHITESPACE.sub(" ", unescape(value)).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _extract(html: str, page_url: httpx.URL, original_url: str) -> EmbedIn | None:
    parser = _MetaParser()
    try:
        # Feed in slices so parsing can stop at </head> on large pages.
        for start in range(0, len(html), 8192):
            parser.feed(html[start:start + 8192])
            if parser.done:
                break
    except Exception:
        logger.debug("Unparseable HTML from %s", page_url.host, exc_info=True)

    def first(*keys: str) -> str | None:
        return next((parser.meta[k] for k in keys if parser.meta.get(k, "").strip()), None)

    title = _clean(first("og:title", "twitter:title") or parser.title, 300)
    description = _clean(first("og:description", "twitter:description", "description"), 1000)
    if not title and not description:
        return None

    image_url = None
    image = first("og:image", "og:image:secure_url", "twitter:image")
    if image and image.strip():
        candidate = urljoin(str(page_url), unescape(image.strip()))
        if len(candidate) <= MAX_URL_LENGTH and candidate.lower().startswith(("http://", "https://")):
            image_url = candidate

    return EmbedIn(
        url=original_url,
        site_name=_clean(first("og:site_name"), 100),
        title=title,
        description=description,
        image_url=image_url,
    )


_cache: OrderedDict[str, tuple[float, EmbedIn | None]] = OrderedDict()


def _cache_get(key: str) -> tuple[bool, EmbedIn | None]:
    entry = _cache.get(key)
    if entry is None:
        return False, None
    expires_at, value = entry
    if expires_at < time.monotonic():
        del _cache[key]
        return False, None
    _cache.move_to_end(key)
    return True, value


def _cache_put(key: str, value: EmbedIn | None) -> None:
    ttl = HIT_TTL_SECONDS if value is not None else MISS_TTL_SECONDS
    _cache[key] = (time.monotonic() + ttl, value)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


async def unfurl(url: str) -> EmbedIn | None:
    """Preview metadata for *url*, or None when there is nothing to show.

    Raises BlockedURL when the URL, a redirect target or a resolved address is
    not allowed. Any other fetch failure yields None.
    """
    parsed = _check_url(url)
    hit, cached = _cache_get(url)
    if hit:
        return cached

    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            page = await _fetch_html(parsed)
    except BlockedURL:
        raise
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        logger.info("Unfurl of %s failed: %s", parsed.host, type(exc).__name__)
        page = None

    embed = _extract(*page, url) if page else None
    _cache_put(url, embed)
    return embed
