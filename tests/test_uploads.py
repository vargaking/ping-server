"""ZET-12: image upload validation and normalisation."""
from io import BytesIO

import pytest
from PIL import Image

from app.services.storage import storage_service
from tests.conftest import create_server, register


def _png_bytes(size=(1000, 1000), color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _stored_image(url: str) -> Image.Image:
    """Open the file that a returned /media URL points at on disk."""
    rel = url.split("/media/", 1)[1]
    return Image.open(storage_service.root / rel)


# --- avatars (256px) and banners (1200x480) -------------------------------

def _upload(client, kind, content, name="image.png"):
    me = client.get("/auth/me").json()
    return client.post(f"/users/{me['id']}/{kind}", files={"file": (name, content, "image/png")})


def _profile(client):
    return client.get("/auth/me").json()["profile"]


def test_valid_avatar_is_accepted_and_resized(client):
    register(client)
    res = _upload(client, "avatar", _png_bytes((1000, 1000)))
    assert res.status_code == 200, res.text

    url = res.json()["profile"]["avatar"]
    with _stored_image(url) as img:
        assert img.format == "PNG"
        assert max(img.size) <= 256


@pytest.mark.parametrize(
    "source, fitted",
    [
        ((3000, 1000), (1200, 400)),
        ((1000, 3000), (160, 480)),
        ((2000, 800), (1200, 480)),
        ((600, 200), (600, 200)),
    ],
)
def test_banner_is_fitted_into_its_box(client, source, fitted):
    register(client)
    res = _upload(client, "banner", _png_bytes(source))
    assert res.status_code == 200, res.text

    with _stored_image(res.json()["profile"]["banner_image"]) as img:
        assert img.format == "PNG"
        assert img.size == fitted


@pytest.mark.parametrize("kind", ["avatar", "banner"])
def test_oversize_image_returns_413(client, monkeypatch, kind):
    register(client)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "512")
    res = _upload(client, kind, _png_bytes((1000, 1000)))
    assert res.status_code == 413, res.text
    assert _profile(client) == {}


@pytest.mark.parametrize("kind", ["avatar", "banner"])
def test_non_image_returns_415(client, kind):
    register(client)
    # Correct-looking name and MIME, but the bytes are not an image: the server
    # must sniff the real type and reject it.
    res = _upload(client, kind, b"this is definitely not an image")
    assert res.status_code == 415, res.text
    assert _profile(client) == {}


@pytest.mark.parametrize("kind", ["avatar", "banner"])
def test_corrupt_png_returns_415_not_500(client, kind):
    register(client)
    # A real PNG with one byte of pixel data flipped: the chunk CRC no longer
    # matches, which Pillow reports as SyntaxError rather than OSError.
    data = bytearray(_png_bytes((8, 8)))
    idat = data.index(b"IDAT")
    data[idat + 6] ^= 0xFF
    res = _upload(client, kind, bytes(data))
    assert res.status_code == 415, res.text
    assert _profile(client) == {}


# --- server icons (512px) -------------------------------------------------

def test_valid_server_icon_is_accepted_and_resized(client):
    register(client)
    server = create_server(client)
    files = {"file": ("icon.png", _png_bytes((2000, 2000)), "image/png")}
    res = client.post(f"/servers/{server['id']}/icon", files=files)
    assert res.status_code == 200, res.text

    url = res.json()["server_profile"]["icon"]
    with _stored_image(url) as img:
        assert img.format == "PNG"
        assert max(img.size) <= 512
