"""Link-preview endpoint: SSRF guard, parsing, caching, rate limit, and how
embeds travel with messages. No test touches the network: DNS and the TCP layer
are replaced by in-memory fakes."""
import ipaddress

import anyio
import httpcore
import pytest

from app.services import unfurl as unfurl_service
from tests.conftest import register
from tests.test_direct_messages import _recv as recv, dm_frame, open_conversation
from tests.test_websocket import HEADERS, chat_frame, two_members  # noqa: F401
from tests.conftest import ws_ready

PUBLIC_IP = "93.184.216.34"
OG_PAGE = """<html><head>
<title>Fallback title</title>
<meta property="og:title" content="  Hello &amp;   world ">
<meta property="og:description" content="A page">
<meta property="og:site_name" content="Example">
<meta property="og:image" content="/img/cover.png">
<meta name="description" content="plain description">
</head><body>ignored</body></html>"""


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, net, ip):
        self._net = net
        self._ip = ip
        self._written = b""
        self._pending = None

    async def write(self, buffer, timeout=None):
        self._written += buffer
        if b"\r\n\r\n" not in self._written:
            return
        head = self._written.decode("latin-1")
        self._net.requests.append(head)
        host = next(
            line.split(":", 1)[1].strip().rsplit(":", 1)[0] if line.lower().count(":") > 1
            else line.split(":", 1)[1].strip()
            for line in head.split("\r\n") if line.lower().startswith("host:"))
        self._written = b""
        self._pending = self._net.respond(host)

    async def read(self, max_bytes, timeout=None):
        if self._net.hang:
            await anyio.sleep(30)
        if not self._pending:
            return b""
        chunk, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        self._net.bytes_read += len(chunk)
        return chunk

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._net.tls_hosts.append(server_hostname)
        return self

    def get_extra_info(self, info):
        return None


class FakeNet(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.dns = {}
        self.pages = {}
        self.connects = []
        self.tls_hosts = []
        self.requests = []
        self.bytes_read = 0
        self.hang = False
        self.fetches = 0

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        return FakeStream(self, host)

    async def sleep(self, seconds):
        await anyio.sleep(seconds)

    def respond(self, host):
        self.fetches += 1
        status, headers, body = self.pages[host]
        if isinstance(body, str):
            body = body.encode()
        lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", "Connection: close"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    def page(self, host, body=OG_PAGE, status=200, content_type="text/html; charset=utf-8", **headers):
        self.pages[host] = (status, {"Content-Type": content_type, **headers}, body)

    def redirect(self, host, location, status=302):
        self.pages[host] = (status, {"Location": location}, b"")


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()

    async def resolve(host, port):
        if host not in fake.dns:
            raise OSError("no such host")
        return [ipaddress.ip_address(a) for a in fake.dns[host]]

    monkeypatch.setattr(unfurl_service, "_resolve", resolve)
    monkeypatch.setattr(unfurl_service, "_tcp_backend", lambda: fake)
    unfurl_service.clear_cache()
    fake.dns["example.com"] = [PUBLIC_IP]
    fake.page("example.com")
    yield fake
    unfurl_service.clear_cache()


@pytest.fixture
def user(client):
    return register(client)


def get_unfurl(client, url):
    return client.get("/unfurl", params={"url": url})


def test_logged_out_is_401(client, net):
    assert get_unfurl(client, "https://example.com/").status_code == 401


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/",
    "http://127.0.0.1/",
    "http://192.168.1.1",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/",
    "http://10.0.0.1",
    "http://172.16.0.1",
    "http://100.64.0.1",
    "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[::7f00:1]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://user:pw@example.com/",
    "http://example.com:22/",
    "http://example.com:8080/",
    "not a url",
    "http:///path",
])
def test_blocked_urls_are_400_before_any_network(client, user, net, url):
    res = get_unfurl(client, url)

    assert res.status_code == 400
    assert res.json() == {"detail": "URL not allowed"}
    assert net.connects == []


@pytest.mark.parametrize("address", ["10.1.2.3", "127.0.0.1", "::1", "::ffff:192.168.0.1"])
def test_hostname_resolving_to_private_address_is_400(client, user, net, address):
    net.dns["evil.test"] = [address]
    net.page("evil.test")

    assert get_unfurl(client, "http://evil.test/").status_code == 400
    assert net.connects == []


def test_any_private_answer_blocks_even_with_public_ones(client, user, net):
    net.dns["mixed.test"] = [PUBLIC_IP, "10.0.0.5"]
    net.page("mixed.test")

    assert get_unfurl(client, "http://mixed.test/").status_code == 400
    assert net.connects == []


def test_dns_that_changes_between_lookups_cannot_rebind(client, user, net, monkeypatch):
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

    async def flipping(host, port):
        return [ipaddress.ip_address(a) for a in next(answers)]

    monkeypatch.setattr(unfurl_service, "_resolve", flipping)
    net.page("rebind.test")
    res = get_unfurl(client, "http://rebind.test/")

    assert res.status_code == 200
    assert net.connects == [(PUBLIC_IP, 80)]


def test_connects_to_the_checked_ip_but_speaks_the_hostname(client, user, net):
    res = get_unfurl(client, "https://example.com/page")

    assert res.status_code == 200
    assert net.connects == [(PUBLIC_IP, 443)]
    assert net.tls_hosts == ["example.com"]
    request = net.requests[0]
    assert request.startswith("GET /page HTTP/1.1")
    assert "Host: example.com" in request
    assert PUBLIC_IP not in request
    assert "User-Agent: Mozilla/5.0 (compatible; ZetaLinkPreview/1.0)" in request


def test_plain_http_has_no_tls_and_pins_the_ip(client, user, net):
    assert get_unfurl(client, "http://example.com/").status_code == 200
    assert net.connects == [(PUBLIC_IP, 80)]
    assert net.tls_hosts == []


def test_proxy_environment_is_ignored(client, user, net, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")

    assert get_unfurl(client, "https://example.com/").status_code == 200
    assert net.connects == [(PUBLIC_IP, 443)]


def test_redirect_to_localhost_is_400(client, user, net):
    net.redirect("example.com", "http://localhost/admin")
    net.dns["localhost"] = ["127.0.0.1"]
    net.page("localhost")

    assert get_unfurl(client, "https://example.com/").status_code == 400
    assert net.connects == [(PUBLIC_IP, 443)]


def test_redirect_to_private_literal_ip_is_400(client, user, net):
    net.redirect("example.com", "http://169.254.169.254/")

    assert get_unfurl(client, "http://example.com/").status_code == 400
    assert len(net.connects) == 1


def test_redirect_to_hostname_with_private_address_is_400(client, user, net):
    net.redirect("example.com", "http://internal.test/")
    net.dns["internal.test"] = ["10.0.0.9"]
    net.page("internal.test")

    assert get_unfurl(client, "http://example.com/").status_code == 400


def test_redirect_to_bad_scheme_or_port_is_400(client, user, net):
    net.redirect("example.com", "ftp://example.com/x")
    assert get_unfurl(client, "http://example.com/").status_code == 400
    unfurl_service.clear_cache()
    net.redirect("example.com", "http://example.com:2375/x")
    assert get_unfurl(client, "http://example.com/").status_code == 400


def test_relative_and_followed_redirects(client, user, net):
    net.redirect("example.com", "http://other.test/final", status=301)
    net.dns["other.test"] = ["93.184.216.35"]
    net.page("other.test")

    res = get_unfurl(client, "http://example.com/start")

    assert res.status_code == 200
    assert res.json()["url"] == "http://example.com/start"
    assert res.json()["image_url"] == "http://other.test/img/cover.png"


def test_three_redirects_are_followed_four_are_not(client, user, net):
    net.pages["example.com"] = (302, {"Location": "/next"}, b"")
    assert get_unfurl(client, "http://example.com/").status_code == 204
    assert len(net.requests) == 4


def test_three_redirects_then_page_succeeds(client, user, net):
    hops = iter(["/b", "/c", None])
    original = net.respond

    def respond(host):
        target = next(hops)
        if target is None:
            return original(host)
        return f"HTTP/1.1 302 X\r\nLocation: {target}\r\nContent-Length: 0\r\n\r\n".encode()

    net.respond = respond
    assert get_unfurl(client, "http://example.com/a").status_code == 200


def test_og_tags_become_an_embed(client, user, net):
    res = get_unfurl(client, "https://example.com/post")

    assert res.status_code == 200
    assert res.json() == {
        "url": "https://example.com/post",
        "site_name": "Example",
        "title": "Hello & world",
        "description": "A page",
        "image_url": "https://example.com/img/cover.png",
    }


def test_javascript_og_image_is_dropped(client, user, net):
    net.page("example.com", '<head><meta property="og:title" content="T">'
                            '<meta property="og:image" content="javascript:alert(1)"></head>')
    res = get_unfurl(client, "https://example.com/")

    assert res.status_code == 200
    assert res.json()["image_url"] is None


def test_data_uri_og_image_is_dropped(client, user, net):
    net.page("example.com", '<head><meta property="og:title" content="T">'
                            '<meta property="og:image" content="data:image/png;base64,AAAA"></head>')
    assert get_unfurl(client, "https://example.com/").json()["image_url"] is None


def test_twitter_and_plain_tags_are_fallbacks(client, user, net):
    net.page("example.com", '<head><meta name="twitter:title" content="Tw">'
                            '<meta name="description" content="Plain"></head>')
    body = get_unfurl(client, "https://example.com/").json()

    assert body["title"] == "Tw"
    assert body["description"] == "Plain"


def test_title_only_page(client, user, net):
    net.page("example.com", "<html><head><title>Just a title</title></head></html>")
    body = get_unfurl(client, "https://example.com/").json()

    assert body["title"] == "Just a title"
    assert body["description"] is None
    assert body["site_name"] is None
    assert body["image_url"] is None


def test_page_with_nothing_usable_is_204(client, user, net):
    net.page("example.com", "<html><head></head><body>hi</body></html>")
    assert get_unfurl(client, "https://example.com/").status_code == 204


def test_meta_after_head_is_ignored(client, user, net):
    net.page("example.com", '<head></head><body><meta property="og:title" content="late"></body>')
    assert get_unfurl(client, "https://example.com/").status_code == 204


def test_long_fields_are_truncated(client, user, net):
    net.page("example.com", f'<head><meta property="og:title" content="{"word " * 200}">'
                            f'<meta property="og:description" content="{"x" * 5000}"></head>')
    body = get_unfurl(client, "https://example.com/").json()

    assert len(body["title"]) <= 300 and body["title"].endswith("…")
    assert len(body["description"]) == 1000


def test_non_html_is_204(client, user, net):
    net.page("example.com", b"\x89PNG", content_type="image/png")
    assert get_unfurl(client, "https://example.com/a.png").status_code == 204


@pytest.mark.parametrize("status", [404, 500])
def test_upstream_errors_are_204(client, user, net, status):
    net.page("example.com", status=status)
    assert get_unfurl(client, "https://example.com/").status_code == 204


def test_unresolvable_host_is_204(client, user, net):
    assert get_unfurl(client, "https://nope.test/").status_code == 204


def test_oversized_body_is_cut_not_failed(client, user, net):
    head = '<head><meta property="og:title" content="Big"></head>'
    net.page("example.com", head + "x" * (2 * 1024 * 1024))

    res = get_unfurl(client, "https://example.com/")

    assert res.status_code == 200
    assert res.json()["title"] == "Big"
    assert net.bytes_read < 600 * 1024


def test_slow_server_times_out_to_204(client, user, net, monkeypatch):
    monkeypatch.setattr(unfurl_service, "TIMEOUT_SECONDS", 0.2)
    net.hang = True
    assert get_unfurl(client, "https://example.com/").status_code == 204


def test_second_call_is_served_from_cache(client, user, net):
    assert get_unfurl(client, "https://example.com/p").status_code == 200
    assert get_unfurl(client, "https://example.com/p").status_code == 200
    assert net.fetches == 1


def test_misses_are_cached_too(client, user, net):
    net.page("example.com", "<html></html>")
    assert get_unfurl(client, "https://example.com/p").status_code == 204
    assert get_unfurl(client, "https://example.com/p").status_code == 204
    assert net.fetches == 1


def test_blocked_urls_are_not_cached(client, user, net):
    net.dns["flaky.test"] = ["10.0.0.1"]
    net.page("flaky.test")
    assert get_unfurl(client, "http://flaky.test/").status_code == 400
    net.dns["flaky.test"] = [PUBLIC_IP]
    assert get_unfurl(client, "http://flaky.test/").status_code == 200


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(unfurl_service, "CACHE_SIZE", 3)
    unfurl_service.clear_cache()
    for i in range(5):
        unfurl_service._cache_put(f"u{i}", None)
    assert list(unfurl_service._cache) == ["u2", "u3", "u4"]
    unfurl_service.clear_cache()


def test_overlong_url_is_rejected(client, user, net):
    assert get_unfurl(client, "https://example.com/" + "a" * 2100).status_code == 422


def test_rate_limit_is_per_user(client, new_client, net, monkeypatch):
    monkeypatch.setenv("UNFURL_RATE_LIMIT", "2/minute")
    register(client)

    statuses = [get_unfurl(client, "https://example.com/").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]

    other = new_client()
    register(other)
    assert get_unfurl(other, "https://example.com/").status_code == 200


# --- embeds on messages ----------------------------------------------------

EMBED = {
    "url": "https://example.com/post",
    "site_name": "Example",
    "title": "Hello",
    "description": "A page",
    "image_url": "https://example.com/i.png",
}


def test_channel_message_embed_is_stored_fanned_out_and_in_history(two_members):
    alice_client, _, bob_client, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()

        frame = chat_frame(server["id"], channel["id"], embeds=[EMBED])
        alice_ws.send_json(frame)
        received = bob_ws.receive_json()

    assert received["embeds"] == [EMBED]
    history = bob_client.get(f"/channels/{channel['id']}/messages").json()["messages"]
    assert history[0]["embeds"] == [EMBED]


def send_and_wait(two_members, *frames):
    """Send frames as alice and return what bob receives (so they are stored)."""
    alice_client, _, bob_client, _, _, _ = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()
        received = []
        for frame in frames:
            alice_ws.send_json(frame)
            received.append(bob_ws.receive_json())
    return received


def test_message_without_embed_has_empty_list(two_members):
    alice_client, _, _, _, server, channel = two_members
    (received,) = send_and_wait(two_members, chat_frame(server["id"], channel["id"]))

    assert received["embeds"] == []
    history = alice_client.get(f"/channels/{channel['id']}/messages").json()["messages"]
    assert history[0]["embeds"] == []


@pytest.mark.parametrize("embeds", [
    [EMBED, EMBED],
    [{**EMBED, "url": "javascript:alert(1)"}],
    [{**EMBED, "image_url": "data:image/png;base64,AAAA"}],
    [{**EMBED, "url": "ftp://example.com/"}],
    [{**EMBED, "title": "x" * 301}],
    [{**EMBED, "description": "x" * 1001}],
    [{**EMBED, "site_name": "x" * 101}],
    [{**EMBED, "url": "https://example.com/" + "a" * 2048}],
    [{"title": "no url"}],
    ["https://example.com/"],
])
def test_bad_embeds_are_rejected_as_invalid_frames(two_members, embeds):
    alice_client, _, _, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as ws:
        ws_ready(ws)
        ws.send_json(chat_frame(server["id"], channel["id"], embeds=embeds))
        assert ws.receive_json()["code"] == "invalid_frame"
    history = alice_client.get(f"/channels/{channel['id']}/messages").json()["messages"]
    assert history == []


def test_client_metadata_cannot_smuggle_embeds(two_members):
    alice_client, _, _, _, server, channel = two_members
    forged = {**EMBED, "url": "javascript:alert(1)"}
    only_metadata = chat_frame(
        server["id"], channel["id"], metadata={"embeds": [forged], "other": 1})
    both = chat_frame(
        server["id"], channel["id"], embeds=[EMBED], metadata={"embeds": [forged]})

    received = send_and_wait(two_members, only_metadata, both)

    assert [m["embeds"] for m in received] == [[], [EMBED]]
    history = alice_client.get(f"/channels/{channel['id']}/messages").json()["messages"]
    assert sorted(len(m["embeds"]) for m in history) == [0, 1]
    assert all(m["embeds"] in ([], [EMBED]) for m in history)


def test_extra_embed_fields_are_not_relayed(two_members):
    alice_client, _, bob_client, _, server, channel = two_members
    with alice_client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        ws_ready(alice_ws)
        ws_ready(bob_ws)
        alice_ws.receive_json()
        alice_ws.send_json(chat_frame(
            server["id"], channel["id"], embeds=[{**EMBED, "html": "<script>"}]))
        received = bob_ws.receive_json()
    assert received["embeds"] == [EMBED]


def test_direct_message_embed_roundtrip(client, new_client):
    register(client, "alice-unfurl")
    bob_client = new_client()
    bob = register(bob_client, "bob-unfurl")
    conversation = open_conversation(client, bob["id"])
    with client.websocket_connect("/ws", headers=HEADERS) as alice_ws, \
            bob_client.websocket_connect("/ws", headers=HEADERS) as bob_ws:
        alice_ws.send_json(dm_frame(conversation["id"], embeds=[EMBED]))
        received = recv(bob_ws, "direct_message")

    assert received["embeds"] == [EMBED]
    history = bob_client.get(f"/conversations/{conversation['id']}/messages").json()["messages"]
    assert history[0]["embeds"] == [EMBED]


def test_editing_keeps_embeds(two_members):
    alice_client, _, _, _, server, channel = two_members
    frame = chat_frame(server["id"], channel["id"], embeds=[EMBED])
    send_and_wait(two_members, frame)

    res = alice_client.patch(f"/messages/{frame['id']}", json={"content": "edited"})

    assert res.status_code == 200, res.text
    assert res.json()["embeds"] == [EMBED]


@pytest.mark.parametrize("address, public", [
    ("93.184.216.34", True),
    ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("::ffff:93.184.216.34", True),
    ("10.0.0.1", False),
    ("::ffff:10.0.0.1", False),
    ("2002:7f00:1::", False),
    ("64:ff9b::a00:1", False),
    ("fc00::1", False),
    ("ff02::1", False),
    ("224.0.0.1", False),
    ("::", False),
])
def test_is_public(address, public):
    assert unfurl_service._is_public(ipaddress.ip_address(address)) is public
