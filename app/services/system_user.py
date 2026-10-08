"""The account that owns imported history whose author has no Zet user."""

IMPORTED_USERNAME = "[imported]"
# Not a bcrypt hash, so no password can ever match it.
IMPORTED_PASSWORD_HASH = "!"
