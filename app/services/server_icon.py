import unicodedata
from typing import Optional

import regex

ICON_TEXT_MAX_GRAPHEMES = 2
ICON_TEXT_MAX_CODE_POINTS = 32
ICON_TONE_MIN = 1
ICON_TONE_MAX = 6


def clean_icon_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        return None
    # ZWJ is a format character (Cf) and must stay valid for emoji sequences.
    if any(unicodedata.category(c) in ("Cc", "Zs", "Zl", "Zp") for c in text):
        raise ValueError("Icon text can't contain spaces or control characters")
    if len(text) > ICON_TEXT_MAX_CODE_POINTS or len(regex.findall(r"\X", text)) > ICON_TEXT_MAX_GRAPHEMES:
        raise ValueError(f"Icon text can be at most {ICON_TEXT_MAX_GRAPHEMES} characters")
    return text


def clean_icon_tone(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if not ICON_TONE_MIN <= value <= ICON_TONE_MAX:
        raise ValueError(f"Icon tone must be between {ICON_TONE_MIN} and {ICON_TONE_MAX}")
    return value
