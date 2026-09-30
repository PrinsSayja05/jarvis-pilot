"""MinIO client for storing run diff artifacts (bucket: jarvis-artifacts)."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

from minio import Minio

from jarvis.config import MinioSettings

_BUCKET = "jarvis-artifacts"


class MinioClient:
    def __init__(self, settings: MinioSettings) -> None:
        endpoint = settings.endpoint.split("://", 1)[-1]
        secure = settings.endpoint.startswith("https://")
        self._client = Minio(
            endpoint,
            access_key=settings.access_key,
            secret_key=settings.secret_key,
            secure=secure,
        )
        if not self._client.bucket_exists(_BUCKET):
            self._client.make_bucket(_BUCKET)

    def save_run_artifact(
        self,
        run_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        object_key = f"runs/{run_id}/{filename}"
        self._client.put_object(
            _BUCKET,
            object_key,
            BytesIO(content),
            length=len(content),
            content_type=content_type,
        )
        return object_key

    def save_object(self, object_key: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        """Any key in the bucket, e.g. feedback/<day>/<file>.json (jarvis.audit)."""
        self._client.put_object(_BUCKET, object_key, BytesIO(content), length=len(content), content_type=content_type)
        return object_key

    def store_diff(self, run_id: str, diff_content: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.save_run_artifact(
            run_id, f"{timestamp}-diff.patch", diff_content.encode("utf-8"), "text/x-diff"
        )
