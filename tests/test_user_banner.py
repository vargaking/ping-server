"""Profile banners: the colour is set on PUT /users/{id}, the image through its own upload."""
from io import BytesIO

import pytest
from PIL import Image

from app.models.User import User
from app.services.storage import storage_service
from tests.conftest import register
from tests.test_permissions import run

COLOR_RULE = "Use a colour like #1a2b3c."
IMAGE_URL = "https://example.com/banner.png"


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _files() -> dict:
    return {"file": ("banner.png", _png_bytes(), "image/png")}


def profile_of(client, user_id: int) -> dict:
    async def load():
        return (await User.get(id=user_id)).profile

    return run(client, load)


def seed_avatar(client, user_id: int) -> str:
    res = client.post(f"/users/{user_id}/avatar", files=_files())
    assert res.status_code == 200, res.text
    return res.json()["profile"]["avatar"]


@pytest.mark.parametrize("sent", ["#1a2b3c", "#1A2B3C"])
def test_banner_color_is_stored_lowercase(client, sent):
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"banner_color": sent})
    assert res.status_code == 200, res.text
    assert res.json()["profile"] == {"banner_color": "#1a2b3c"}
    assert profile_of(client, me["id"]) == {"banner_color": "#1a2b3c"}


@pytest.mark.parametrize(
    "value", ["red", "#abc", "#12345g", "#1234567", "", " #123456", 123, ["#123456"]]
)
def test_invalid_banner_colors_are_refused_on_the_field(client, value):
    me = register(client)
    avatar = seed_avatar(client, me["id"])

    res = client.put(f"/users/{me['id']}", json={"username": "should-not-stick", "banner_color": value})
    assert res.status_code == 422
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "banner_color"]
    if isinstance(value, str):
        assert error["type"] == "banner_color"
        assert error["msg"] == COLOR_RULE
    else:
        assert error["type"] == "string_type"
    assert profile_of(client, me["id"]) == {"avatar": avatar}
    assert client.get(f"/users/{me['id']}").json()["username"] == me["username"]


def _put_color(client, uid):
    return client.put(f"/users/{uid}", json={"banner_color": "#aabbcc"})


def _put_null(client, uid):
    return client.put(f"/users/{uid}", json={"banner_color": None})


def _post_banner(client, uid):
    return client.post(f"/users/{uid}/banner", files=_files())


def _delete_banner(client, uid):
    return client.delete(f"/users/{uid}/banner")


def _put_username_only(client, uid):
    return client.put(f"/users/{uid}", json={"username": "renamed-banner"})


@pytest.mark.parametrize(
    "start, action, expected",
    [
        ("color", _put_color, {"banner_color": "#aabbcc"}),
        ("image", _put_color, {"banner_color": "#aabbcc"}),
        ("color", _post_banner, {"banner_image"}),
        ("color", _put_null, {}),
        ("image", _put_null, {}),
        ("image", _delete_banner, {}),
        ("color", _delete_banner, {}),
        ("none", _delete_banner, {}),
        ("color", _put_username_only, {"banner_color": "#112233"}),
    ],
    ids=[
        "color-set-color",
        "image-set-color",
        "color-upload",
        "color-null",
        "image-null",
        "image-delete",
        "color-delete",
        "none-delete",
        "color-rename-only",
    ],
)
def test_banner_changes(client, start, action, expected):
    me = register(client)
    uid = me["id"]
    avatar = seed_avatar(client, uid)
    if start == "color":
        assert client.put(f"/users/{uid}", json={"banner_color": "#112233"}).status_code == 200
    elif start == "image":
        assert _post_banner(client, uid).status_code == 200

    res = action(client, uid)
    assert res.status_code == 200, res.text

    profile = profile_of(client, uid)
    assert res.json()["profile"] == profile
    assert profile.pop("avatar") == avatar
    if isinstance(expected, set):
        assert set(profile) == expected
        assert profile["banner_image"].startswith(f"{storage_service.base_url}/users/{uid}/banner-")
        assert profile["banner_image"].endswith(".png")
    else:
        assert profile == expected


@pytest.mark.parametrize(
    "body",
    [
        {"banner_image": IMAGE_URL},
        {"profile": {"banner_image": IMAGE_URL}},
        {"profile": {"banner_color": "#112233"}},
    ],
    ids=["top-level-image", "profile-image", "profile-color"],
)
def test_banner_cannot_be_set_from_other_fields(client, body):
    me = register(client)
    avatar = seed_avatar(client, me["id"])
    res = client.put(f"/users/{me['id']}", json=body)
    assert res.status_code == 200, res.text
    assert profile_of(client, me["id"]) == {"avatar": avatar}


@pytest.mark.parametrize(
    "action", [_put_color, _post_banner, _delete_banner], ids=["put_color", "post_banner", "delete_banner"]
)
def test_only_the_owner_can_change_a_banner(client, new_client, action):
    other_client = new_client()
    other = register(other_client)
    assert other_client.put(f"/users/{other['id']}", json={"banner_color": "#112233"}).status_code == 200
    register(client)

    res = action(client, other["id"])
    assert res.status_code == 403
    assert profile_of(client, other["id"]) == {"banner_color": "#112233"}


@pytest.mark.parametrize(
    "upload, other_key",
    [("avatar", "banner_color"), ("banner", "avatar")],
)
def test_an_upload_keeps_keys_written_while_it_ran(client, monkeypatch, upload, other_key):
    me = register(client)
    uid = me["id"]
    url = f"/media/users/{uid}/{upload}-fake.png"

    async def upload_that_races(*args, **kwargs):
        await User.filter(id=uid).update(profile={other_key: "written-meanwhile"})
        return url

    monkeypatch.setattr(storage_service, "upload_image", upload_that_races)
    res = client.post(f"/users/{uid}/{upload}", files=_files())
    assert res.status_code == 200, res.text

    own_key = "avatar" if upload == "avatar" else "banner_image"
    assert profile_of(client, uid) == {own_key: url, other_key: "written-meanwhile"}
