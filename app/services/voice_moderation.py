from ..permissions import Permission

SERVER_MUTED_ATTRIBUTE = "server_muted"


class VoiceModeration:
    """Who is server-muted, per (server, user). Held in memory, so mutes are
    forgotten on restart."""

    def __init__(self) -> None:
        self._muted: set[tuple[int, int]] = set()

    def is_muted(self, server_id: int, user_id: int) -> bool:
        return (server_id, user_id) in self._muted

    def set_muted(self, server_id: int, user_id: int, muted: bool) -> None:
        if muted:
            self._muted.add((server_id, user_id))
        else:
            self._muted.discard((server_id, user_id))


def publish_sources(effective: Permission, server_muted: bool) -> list[str]:
    sources = []
    if effective & Permission.SPEAK and not server_muted:
        sources.append("microphone")
    if effective & Permission.STREAM:
        sources += ["screen_share", "screen_share_audio"]
    return sources


def server_muted_attributes(server_muted: bool) -> dict[str, str]:
    # LiveKit deletes an attribute whose value is empty.
    return {SERVER_MUTED_ATTRIBUTE: "true" if server_muted else ""}
