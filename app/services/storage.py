import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("app.services.storage")


class StorageService:
    """Handles file uploads to local disk.

    Files are written under MEDIA_ROOT, preserving the logical destination path
    (e.g. ``users/1/avatar``) with a uuid prefix to avoid collisions, and served
    back via MEDIA_BASE_URL. Serving can be done by FastAPI's StaticFiles mount
    (default, see app.py) or by Nginx pointing at MEDIA_ROOT for better perf.

    Env vars:
        MEDIA_ROOT      Directory to store uploads (default: ./media)
        MEDIA_BASE_URL  Public URL prefix mapping to MEDIA_ROOT
                        (default: /media — works with the app's StaticFiles mount)
    """

    def __init__(self) -> None:
        self.root = Path(os.getenv("MEDIA_ROOT", "media")).resolve()
        # No trailing slash, so we can join with "/" cleanly.
        self.base_url = os.getenv("MEDIA_BASE_URL", "/media").rstrip("/")

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.enabled = True
        except Exception:
            logger.error("Failed to initialize local StorageService at %s", self.root, exc_info=True)
            self.enabled = False

    def _extension_for(self, content_type: Optional[str], destination_path: str) -> str:
        # Prefer an extension already present on the destination path.
        suffix = Path(destination_path).suffix
        if suffix:
            return suffix
        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                return guessed
        return ""

    async def upload_file(self, file_content: bytes, content_type: str, destination_path: str) -> Optional[str]:
        if not self.enabled:
            logger.warning("StorageService not initialized — skipping upload")
            return None

        try:
            # Namespace the file under its logical path with a uuid, keeping the
            # directory structure (users/<id>/, servers/<id>/) on disk.
            dest = Path(destination_path)
            ext = self._extension_for(content_type, destination_path)
            filename = f"{dest.name}-{uuid.uuid4().hex}{ext}"
            rel_dir = dest.parent  # e.g. users/1

            abs_dir = (self.root / rel_dir).resolve()
            # Guard against path traversal in destination_path.
            if not str(abs_dir).startswith(str(self.root)):
                logger.error("Rejected upload outside MEDIA_ROOT: %s", destination_path)
                return None
            abs_dir.mkdir(parents=True, exist_ok=True)

            abs_path = abs_dir / filename
            abs_path.write_bytes(file_content)

            # Build the public URL: base + relative path from MEDIA_ROOT.
            rel_url_path = abs_path.relative_to(self.root).as_posix()
            return f"{self.base_url}/{rel_url_path}"
        except Exception:
            logger.error("Failed to store file for %s", destination_path, exc_info=True)
            return None


storage_service = StorageService()
