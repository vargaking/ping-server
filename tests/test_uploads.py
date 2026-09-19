"""ZET-12: image upload validation and normalisation."""
from io import BytesIO

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


# --- avatars (256px) ------------------------------------------------------

def test_valid_avatar_is_accepted_and_resized(client):
    me = register(client)
    files = {"file": ("avatar.png", _png_bytes((1000, 1000)), "image/png")}
    res = client.post(f"/users/{me['id']}/avatar", files=files)
    assert res.status_code == 200, res.text

    url = res.json()["profile"]["avatar"]
    with _stored_image(url) as img:
        assert img.format == "PNG"
        assert max(img.size) <= 256


def test_oversize_avatar_returns_413(client, monkeypatch):
    me = register(client)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "512")
    files = {"file": ("avatar.png", _png_bytes((1000, 1000)), "image/png")}
    res = client.post(f"/users/{me['id']}/avatar", files=files)
    assert res.status_code == 413, res.text


def test_non_image_avatar_returns_415(client):
    me = register(client)
    # Correct-looking name and MIME, but the bytes are not an image: the server
    # must sniff the real type and reject it.
    files = {"file": ("avatar.png", b"this is definitely not an image", "image/png")}
    res = client.post(f"/users/{me['id']}/avatar", files=files)
    assert res.status_code == 415, res.text


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
