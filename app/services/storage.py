import logging
import mimetypes
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("app.services.storage")

# Upload validation.
_DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
# Real (sniffed) formats we accept, mapped from Pillow's Image.format names.
_ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}


def _max_upload_bytes() -> int:
    """Read per call so a test can shrink the limit via env."""
    try:
        return int(os.getenv("MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES))
    except ValueError:
        return _DEFAULT_MAX_UPLOAD_BYTES


class ImageValidationError(Exception):
    """Base class for rejected image uploads. Carries the HTTP status a router
    should surface (413 too large / 415 wrong type)."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ImageTooLargeError(ImageValidationError):
    status_code = 413


class UnsupportedImageTypeError(ImageValidationError):
    status_code = 415


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

    async def upload_image(
        self, file_content: bytes, destination_path: str, *, max_size_px: int
    ) -> Optional[str]:
        """Validate, normalise and store an image upload.

        - rejects anything over the byte limit (413 via ImageTooLargeError)
        - sniffs the *real* format with Pillow and rejects non-allowlisted types
          (415 via UnsupportedImageTypeError) — the client-declared MIME/header
          is never trusted
        - re-encodes to PNG, bounded to a ``max_size_px`` square, which both
          resizes and strips any non-image payload smuggled in the file

        Returns the public URL, or ``None`` if the underlying write fails.
        """
        limit = _max_upload_bytes()
        if len(file_content) > limit:
            raise ImageTooLargeError(
                f"File too large: {len(file_content)} bytes exceeds the "
                f"{limit}-byte limit"
            )

        # Sniff the true type by actually parsing the bytes. verify() detects
        # truncated/corrupt files; .format gives the real container.
        try:
            with Image.open(BytesIO(file_content)) as probe:
                fmt = probe.format
                probe.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            raise UnsupportedImageTypeError(
                "Unsupported or corrupt image; allowed types: PNG, JPEG, WEBP, GIF"
            )

        if fmt not in _ALLOWED_IMAGE_FORMATS:
            raise UnsupportedImageTypeError(
                f"Unsupported image type {fmt!r}; allowed types: PNG, JPEG, WEBP, GIF"
            )

        # verify() leaves the image unusable, so reopen for the actual re-encode.
        try:
            with Image.open(BytesIO(file_content)) as img:
                img = img.convert("RGBA")
                # Bound within a max_size_px square, preserving aspect ratio.
                img.thumbnail((max_size_px, max_size_px))
                buffer = BytesIO()
                img.save(buffer, format="PNG")
        except (UnidentifiedImageError, OSError, ValueError):
            raise UnsupportedImageTypeError(
                "Unsupported or corrupt image; allowed types: PNG, JPEG, WEBP, GIF"
            )

        return await self.upload_file(buffer.getvalue(), "image/png", destination_path)


storage_service = StorageService()
