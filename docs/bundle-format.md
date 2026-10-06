# Server bundle format

A bundle is a folder an exporter writes and `python -m app.scripts.import_bundle` reads. This is format version 1. A sample lives in `tests/fixtures/bundle/`.

```
<bundle>/
  server.json
  channels/<channel id>/messages/<n>.json   channel messages, oldest first, in chunks
  channels/<channel id>/threads/<id>.json   forum posts, and threads inside text channels
  files/<attachment id>/<filename>
  avatars/<author id>.<ext>
  emoji/<emoji id>.<ext>
```

All ids are the source's ids, as strings. Times are ISO 8601 UTC. Readers ignore fields they don't know.

## server.json

| Field | Content |
| --- | --- |
| `format` | `1`. Anything else is refused. |
| `source` | `{platform, server_id, server_name, exported_at}` |
| `categories` | `[{id, name, position}]` |
| `channels` | `[{id, name, type, topic, category_id, position, private, tags}]`. `type` is `text`, `voice` or `forum`. `tags` is `[{id, name}]` and only used by forums. |
| `authors` | `[{id, name, avatar, messages}]` |
| `emoji` | `[{id, name, path}]` |
| `unreadable` | `[{id, name}]`: channels the exporter couldn't read. They are listed in `channels` too and have no files. |

## Messages

A chunk file is a JSON array of messages. Chunks are read in numeric order (`2.json` before `10.json`).

```
{id, author_id, timestamp, edited_at, content, reply_to_id, pinned, attachments, embeds, reactions}
```

- `content` is markdown. A user mention is `<@author id>`. Everything else platform specific is already plain text.
- `attachments`: `[{id, filename, size, content_type, path}]`. `path` is relative to the bundle root, for example `files/<attachment id>/<filename>`, and `null` when the file wasn't downloaded.
- `embeds`: `[{url, title, description, site_name, image_url}]`
- `reactions`: `[{emoji, count}]`

## Threads

A thread file holds one forum post, or one thread of a text channel:

```
{id, title, tag_ids, pinned, locked, created_at, messages}
```

`messages` are oldest first and the first one is the opening message.

## Importing

```
python -m app.scripts.import_bundle <bundle> --server <server id> --authors authors.json \
    [--only <channel id> ...] [--map <channel id>=<channel id> ...] [--include-private] [--dry-run]
```

`authors.json` maps source author ids to usernames: `{"123": "alice"}`. Authors without an entry are owned by the `[imported]` account, which can't log in.

Run with `--dry-run` first: it prints what each channel would do and writes nothing. Channels match in this order: a `--map` entry, a channel an earlier run of the same bundle created or filled, a channel with the same name and type, otherwise a new one. Running again skips what exists, continues where a failed run stopped, and hands messages to authors mapped since. A bundle can be imported into more than one server; each import is independent.

Not imported yet, and counted in the report: private channels (without `--include-private`), threads inside text channels, reactions, pins on channel messages, custom emoji, avatars, and the text chat of voice channels.

## Importing from Server settings

The server owner can upload the bundle as a zip instead of running the script. Zip the bundle folder; `server.json` can be at the root of the zip or in one folder inside it.

Only `server.json`, `channels/<id>/messages/*.json`, `channels/<id>/threads/*.json` and `files/<id>/<name>` are unpacked. Everything else in the zip is ignored, and so are attachment files over `MAX_ATTACHMENT_BYTES`, which the plan counts as over the limit. The upload is checked with the same dry run as `--dry-run`, then imported by the same importer, without private channels. Once imported, the files are removed and the data stays, so authors can be mapped to members later.
