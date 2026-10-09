"""PUT /users/{id}: only the username and the public key can be changed there."""
from io import BytesIO

import pytest
from PIL import Image

from app.models.User import User
from tests.conftest import PASSWORD, register
from tests.test_permissions import run

USERNAME_RULE = "Use 3 to 32 characters: letters, numbers, dots, dashes and underscores."


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def stored(client, user_id: int) -> User:
    async def load():
        return await User.get(id=user_id)

    return run(client, load)


def test_rename_to_a_free_name(client):
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"username": "new.name-1_"})
    assert res.status_code == 200, res.text
    assert res.json()["username"] == "new.name-1_"
    assert client.get("/auth/me").json()["username"] == "new.name-1_"


@pytest.mark.parametrize("username", ["ab", "a" * 33, "with space", "at@sign", "", "[imported]"])
def test_invalid_usernames_are_refused_with_a_readable_message(client, username):
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"username": username})
    assert res.status_code == 422
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "username"]
    assert error["msg"] == USERNAME_RULE
    assert stored(client, me["id"]).username == me["username"]


def test_a_taken_username_is_a_409_on_the_field(client, new_client):
    taken = register(new_client(), "taken-name")["username"]
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"username": taken})
    assert res.status_code == 409
    assert res.json()["detail"] == [
        {"loc": ["body", "username"], "msg": "That username is taken.", "type": "username_taken"}
    ]


def test_keeping_the_same_username_is_fine(client):
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"username": me["username"]})
    assert res.status_code == 200, res.text


def test_a_password_in_the_body_is_ignored(client):
    me = register(client)
    res = client.put(f"/users/{me['id']}", json={"password": "hacked123"})
    assert res.status_code == 200, res.text
    user = stored(client, me["id"])
    assert user.check_password(PASSWORD)
    assert not user.check_password("hacked123")


def test_a_profile_in_the_body_is_ignored_and_the_avatar_upload_still_works(client):
    me = register(client)
    files = {"file": ("avatar.png", _png_bytes(), "image/png")}
    avatar = client.post(f"/users/{me['id']}/avatar", files=files).json()["profile"]["avatar"]

    res = client.put(
        f"/users/{me['id']}", json={"profile": {"avatar": "https://example.com/x.png", "big": "x" * 10_000}}
    )
    assert res.status_code == 200, res.text
    assert stored(client, me["id"]).profile == {"avatar": avatar}


def test_the_current_frontend_sending_the_whole_user_still_renames(client):
    me = register(client)
    body = {**me, "username": "renamed-whole", "profile": {"avatar": "https://example.com/x.png"}}
    res = client.put(f"/users/{me['id']}", json=body)
    assert res.status_code == 200, res.text
    user = stored(client, me["id"])
    assert user.username == "renamed-whole"
    assert user.profile == {}


def test_updating_someone_else_is_still_403(client, new_client):
    other = register(new_client())
    register(client)
    res = client.put(f"/users/{other['id']}", json={"username": "stolen-name"})
    assert res.status_code == 403


def test_registration_uses_the_same_rule(client):
    res = client.post("/auth/register", json={"username": "[imported]", "password": PASSWORD})
    assert res.status_code == 422
    assert res.json()["detail"][0]["msg"] == USERNAME_RULE
