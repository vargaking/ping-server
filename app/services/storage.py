import os
from google.cloud import storage
import uuid
from typing import Optional


class StorageService:
    def __init__(self):
        self.bucket_name = os.getenv("GOOGLE_STORAGE_BUCKET")
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        self.client = None
        self.bucket = None

        if self.bucket_name and self.project_id:
            try:
                self.client = storage.Client(project=self.project_id)
                self.bucket = self.client.bucket(self.bucket_name)
            except Exception as e:
                print(f"Failed to initialize StorageService: {e}")

    async def upload_file(self, file_content: bytes, content_type: str, destination_path: str) -> Optional[str]:
        if not self.bucket:
            print("StorageService not initialized")
            return None

        try:
            # Generate a unique filename to prevent caching issues on updates
            # while allowing caching for the specific URL
            filename = f"{uuid.uuid4()}-{destination_path}"
            blob = self.bucket.blob(filename)

            blob.upload_from_string(
                file_content,
                content_type=content_type
            )

            # Return the public URL
            return blob.public_url
        except Exception as e:
            print(f"Failed to upload file: {e}")
            return None


storage_service = StorageService()
