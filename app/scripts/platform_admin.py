"""Grant or revoke platform admin: python -m app.scripts.platform_admin <username> --grant|--revoke"""
import argparse
import asyncio
import sys

from tortoise import Tortoise

from app.db import TORTOISE_CONFIG
from app.models.User import User


async def set_platform_admin(username: str, granted: bool) -> bool:
    """Set the flag on *username*; False if there is no such user."""
    return await User.filter(username=username).update(is_platform_admin=granted) == 1


async def main(username: str, granted: bool) -> int:
    await Tortoise.init(config=TORTOISE_CONFIG)
    try:
        if not await set_platform_admin(username, granted):
            print(f"No user named {username!r}", file=sys.stderr)
            return 1
    finally:
        await Tortoise.close_connections()
    print("granted" if granted else "revoked")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--grant", action="store_true")
    action.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.username, args.grant)))
