# Backend tests

```bash
pip install -r requirements-dev.txt
pytest
```

- No Postgres needed: `conftest.py` points the app at in-memory SQLite and sets
  `DB_GENERATE_SCHEMAS=true`, so each test starts from an empty schema built from
  the models (Aerich migrations are not exercised here).
- `client` is one logged-out "browser". `new_client()` gives you another one with
  its own cookie jar against the same app/DB, for multi-user scenarios.
- Prefer driving the public API (`register`, `create_server`, `create_channel`
  helpers) over touching models directly.
- WebSockets: `client.websocket_connect("/ws", headers={"origin": ORIGIN})`.
