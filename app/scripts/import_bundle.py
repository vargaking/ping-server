"""Import a server bundle: python -m app.scripts.import_bundle <bundle> --server <id> [--authors authors.json] [--only <channel id> ...] [--map <channel id>=<channel id> ...] [--include-private] [--dry-run]"""
import argparse
import asyncio
import sys
from pathlib import Path

from tortoise import Tortoise

from app.db import TORTOISE_CONFIG
from app.services.bundle.format import BundleError
from app.services.bundle.importer import (
    ImportAborted,
    ImportOptions,
    format_report,
    import_bundle,
    read_authors,
)


def parse_map(entries: list[str]) -> dict[str, int]:
    mapping = {}
    for entry in entries:
        source_id, _, target = entry.partition("=")
        if not source_id or not target.isdigit():
            raise ImportAborted(f"--map expects <source channel id>=<channel id>, got {entry!r}")
        mapping[source_id] = int(target)
    return mapping


async def main(args: argparse.Namespace) -> int:
    try:
        options = ImportOptions(
            server_id=args.server,
            authors=read_authors(args.authors) if args.authors else {},
            only=set(args.only),
            channel_map=parse_map(args.map),
            include_private=args.include_private,
            dry_run=args.dry_run,
        )
        await Tortoise.init(config=TORTOISE_CONFIG)
        try:
            report = await import_bundle(args.bundle, options)
        finally:
            await Tortoise.close_connections()
    except (BundleError, ImportAborted) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--server", type=int, required=True, help="id of the server to import into")
    parser.add_argument("--authors", type=Path, help="JSON file: source author id to username")
    parser.add_argument("--only", nargs="+", action="extend", default=[], metavar="CHANNEL_ID")
    parser.add_argument("--map", nargs="+", action="extend", default=[], metavar="SOURCE=CHANNEL")
    parser.add_argument("--include-private", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
