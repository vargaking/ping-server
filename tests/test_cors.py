"""ZET-59: CORS must echo allowlisted origins *and* Vercel preview subdomains.

With `withCredentials` on the frontend, a wildcard `Access-Control-Allow-Origin`
is illegal — the matched origin has to be echoed back. Previews get randomized
subdomains, so they're matched by regex rather than enumerated. The regex is
anchored on the team slug so another team's Vercel app can't slip through.
"""
from app.settings import is_origin_allowed
from tests.conftest import ORIGIN, PREVIEW_ORIGIN


def _preflight(client, origin):
    return client.options(
        "/auth/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_explicit_origin_is_echoed(client):
    res = _preflight(client, ORIGIN)
    assert res.headers.get("access-control-allow-origin") == ORIGIN
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_preview_origin_is_echoed_by_regex(client):
    res = _preflight(client, PREVIEW_ORIGIN)
    assert res.headers.get("access-control-allow-origin") == PREVIEW_ORIGIN
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_foreign_origin_is_not_echoed(client):
    res = _preflight(client, "https://evil.example")
    assert res.headers.get("access-control-allow-origin") is None


def test_foreign_vercel_origin_is_not_echoed(client):
    # Same host suffix (.vercel.app) but a different team slug must be rejected.
    foreign = "https://ping-frontend-abc123-someone-else-projects.vercel.app"
    res = _preflight(client, foreign)
    assert res.headers.get("access-control-allow-origin") is None


# --- is_origin_allowed: the shared helper the /ws handshake also uses ---------

def test_is_origin_allowed_matches_explicit_and_regex():
    assert is_origin_allowed(ORIGIN)
    assert is_origin_allowed(PREVIEW_ORIGIN)


def test_is_origin_allowed_rejects_foreign_and_substring_tricks():
    assert not is_origin_allowed("https://evil.example")
    # Not anchored end-to-end? A suffix-append attack would pass. It must not.
    assert not is_origin_allowed(
        "https://ping-frontend-abc-vargakings-projects.vercel.app.evil.com"
    )
    # Wrong team slug.
    assert not is_origin_allowed(
        "https://ping-frontend-abc-someone-else-projects.vercel.app"
    )
