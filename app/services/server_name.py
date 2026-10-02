from typing import Optional

SERVER_NAME_MAX = 100


def clean_server_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("Server name can't be empty")
    if len(value) > SERVER_NAME_MAX:
        raise ValueError(f"Server name must be at most {SERVER_NAME_MAX} characters")
    return value
