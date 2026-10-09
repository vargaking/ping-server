import re
from typing import Annotated

from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
_USERNAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_username(value: str) -> str:
    if not USERNAME_MIN_LENGTH <= len(value) <= USERNAME_MAX_LENGTH or not _USERNAME.match(value):
        raise PydanticCustomError(
            "username",
            "Use 3 to 32 characters: letters, numbers, dots, dashes and underscores.",
        )
    return value


# Brackets are outside the pattern, so the importer's "[imported]" account can't be taken.
Username = Annotated[str, AfterValidator(_check_username)]
