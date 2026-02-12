import logging
import os
import uuid
from typing import Optional

from google.cloud import storage

logger = logging.getLogger("app.services.storage")


class StorageService:
    """Handles file uploads to Google Cloud Storage."""

    def __init__(self) -> None:
        self.bucket_name = os.getenv("GOOGLE_STORAGE_BUCKET")
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        self.client = None
        self.bucket = None

        if self.bucket_name and self.project_id:
            try:
                self.client = storage.Client(project=self.project_id)
                self.bucket = self.client.bucket(self.bucket_name)
            except Exception:
                logger.error("Failed to initialize StorageService", exc_info=True)

    async def upload_file(self, file_content: bytes, content_type: str, destination_path: str) -> Optional[str]:
        if not self.bucket:
            logger.warning("StorageService not initialized — skipping upload")
            return None

        try:
            filename = f"{uuid.uuid4()}-{destination_path}"
            blob = self.bucket.blob(filename)

            blob.upload_from_string(
                file_content,
                content_type=content_type
            )

            # Return the public URL
            return blob.public_url
        except Exception:
            logger.error("Failed to upload file to %s", destination_path, exc_info=True)
            return None


storage_service = StorageService()
