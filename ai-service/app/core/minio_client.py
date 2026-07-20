"""MinIO object fetch client for L3 parse workers."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.exceptions import AiException
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class ObjectStore(Protocol):
    """Minimal object-store contract used by the parse handler."""

    def fetch_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        """Download one object as raw bytes.

        Args:
            object_key: S3 object key within the bucket.
            bucket: Optional bucket override.

        Returns:
            Raw object bytes.

        Raises:
            AiException: When the object cannot be fetched.
        """
        ...


class MinioObjectClient:
    """Fetch uploaded PDFs from the finreport-uploads bucket."""

    def __init__(
        self, settings: Settings | None = None, client: Any | None = None
    ) -> None:
        """Create a MinIO-backed object client.

        Args:
            settings: Runtime settings with endpoint and credentials.
            client: Optional pre-built MinIO client for tests.
        """
        self.settings = settings or Settings()
        self._client = client

    def fetch_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        """Download one PDF object from MinIO.

        Args:
            object_key: Object key emitted by L2 upload (e.g. uploads/1/hash/report.pdf).
            bucket: Bucket override; defaults to ``finreport-uploads``.

        Returns:
            Raw PDF bytes.

        Raises:
            AiException: When MinIO is unavailable or the object is missing.
        """
        target_bucket = bucket or self.settings.minio_upload_bucket
        LOGGER.debug(
            "[fetch_bytes] bucket=%s objectKey=%s",
            target_bucket,
            object_key,
        )
        try:
            client = self._ensure_client()
            response = client.get_object(target_bucket, object_key)
            try:
                data = response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception as error:
            raise AiException(
                f"MinIO fetch failed bucket={target_bucket} key={object_key}: {error}"
            ) from error

        if not data:
            raise AiException(
                f"MinIO object is empty bucket={target_bucket} key={object_key}"
            )
        return data

    def _ensure_client(self) -> Any:
        """Lazily build the MinIO SDK client on first use.

        Returns:
            A configured ``Minio`` client instance.

        Raises:
            AiException: When the MinIO SDK is not installed.
        """
        if self._client is not None:
            return self._client
        try:
            from minio import Minio
        except ImportError as error:
            raise AiException("MinIO client (minio package) is required") from error

        endpoint = self.settings.minio_endpoint.strip()
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"http://{endpoint}"
        parsed = urlparse(endpoint)
        host = parsed.netloc or parsed.path
        secure = parsed.scheme == "https"
        self._client = Minio(
            host,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=secure,
        )
        return self._client
