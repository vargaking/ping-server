import unicodedata

from ..models.Reaction import Reaction

MAX_EMOJI_CODE_POINTS = 32
MAX_DISTINCT_EMOJIS_PER_MESSAGE = 20

_ZWJ = 0x200D
_KEYCAP = 0x20E3
_KEYCAP_BASES = frozenset(map(ord, "#*0123456789"))

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x2B00, 0x2BFF),
    (0x1F1E6, 0x1F1FF),
    (0x1F3FB, 0x1F3FF),
    (_ZWJ, _ZWJ),
    (0xFE0E, 0xFE0F),
    (_KEYCAP, _KEYCAP),
    (0xE0020, 0xE007F),
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)


def _in_emoji_ranges(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in _EMOJI_RANGES)


def normalize_emoji(value: str) -> str | None:
    """The NFC form of *value* if it is an acceptable reaction emoji, else None."""
    emoji = unicodedata.normalize("NFC", value)
    if not 1 <= len(emoji) <= MAX_EMOJI_CODE_POINTS:
        return None
    code_points = [ord(char) for char in emoji]
    allow_keycap_bases = _KEYCAP in code_points
    for code_point in code_points:
        if _in_emoji_ranges(code_point):
            continue
        if allow_keycap_bases and code_point in _KEYCAP_BASES:
            continue
        return None
    return emoji


async def reactions_by_message(message_ids: list[int]) -> dict[int, list[dict]]:
    """Wire form of the reactions of each message, in one query. Messages
    without reactions are absent from the result.

    Emojis are ordered by their first reaction, users by reaction time.
    """
    grouped: dict[int, dict[str, list[int]]] = {}
    if not message_ids:
        return {}
    rows = await Reaction.filter(
        message_id__in=message_ids).order_by("created_at", "id")
    for reaction in rows:
        by_emoji = grouped.setdefault(reaction.message_id, {})
        by_emoji.setdefault(reaction.emoji, []).append(reaction.user_id)
    return {
        message_id: [
            {"emoji": emoji, "user_ids": user_ids}
            for emoji, user_ids in by_emoji.items()
        ]
        for message_id, by_emoji in grouped.items()
    }
