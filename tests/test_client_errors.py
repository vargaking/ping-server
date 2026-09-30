import logging

from tests.conftest import register

URL = "/api/client-errors"
REPORT = {"kind": "error", "message": "boom", "stack": "at foo (app.js:1:2)", "url": "https://x.test/chat"}


def _client_records(caplog):
    return [r for r in caplog.records if r.name == "app.client"]


def test_logged_out_report_is_accepted(client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.client"):
        res = client.post(URL, json=REPORT)

    assert res.status_code == 204
    (record,) = _client_records(caplog)
    line = record.getMessage()
    assert "user_id=-" in line
    assert "kind=error" in line and "message=boom" in line


def test_logged_in_report_includes_user_id(client, caplog):
    me = register(client)
    with caplog.at_level(logging.WARNING, logger="app.client"):
        res = client.post(URL, json={**REPORT, "line": 3, "column": 7})

    assert res.status_code == 204
    line = _client_records(caplog)[0].getMessage()
    assert f"user_id={me['id']}" in line
    assert "line=3 col=7" in line


def test_control_characters_cannot_forge_log_lines(client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.client"):
        res = client.post(
            URL,
            json={**REPORT, "message": "a\nWARNING forged\r\x1b[0m", "stack": "s\nt", "url": "u\nv"},
            headers={"User-Agent": "ua\nevil"},
        )

    assert res.status_code == 204
    (record,) = _client_records(caplog)
    line = record.getMessage()
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert "a\\nWARNING forged\\r\\x1b[0m" in line
    assert "ua\\nevil" in line


def test_too_long_message_is_rejected(client):
    res = client.post(URL, json={**REPORT, "message": "x" * 1001})
    assert res.status_code == 422


def test_unknown_kind_is_rejected(client):
    res = client.post(URL, json={**REPORT, "kind": "other"})
    assert res.status_code == 422


def test_unknown_fields_are_ignored(client):
    res = client.post(URL, json={**REPORT, "extra": "x"})
    assert res.status_code == 204


def test_per_ip_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setenv("CLIENT_ERROR_RATE_LIMIT", "3/minute")

    statuses = [client.post(URL, json=REPORT).status_code for _ in range(4)]

    assert statuses == [204, 204, 204, 429]
