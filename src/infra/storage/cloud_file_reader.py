from abc import ABC, abstractmethod
from typing import Optional, BinaryIO
from io import BytesIO
import boto3
from botocore.exceptions import ClientError
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class CloudConfig:
    """Configuration for cloud provider."""

    provider: str
    region: Optional[str] = None


class CloudFileReader(ABC):
    """Abstract base class for cloud file readers."""

    @abstractmethod
    def read_file_to_memory(self, storage_url: str) -> BytesIO:
        """Read file from cloud storage into memory."""
        pass


class AWSFileReader(CloudFileReader):
    """AWS S3 implementation for reading files."""

    def __init__(self, region: Optional[str] = None):
        """
        Initialize AWS S3 reader.

        Args:
            region (Optional[str]): AWS region name. Defaults to None.
        """
        self.s3_client = boto3.client("s3", region_name=region)

    def _parse_s3_url(self, s3_url: str) -> tuple[str, str]:
        """Parse S3 URL into bucket and key."""
        if not s3_url.startswith("s3://"):
            raise ValueError("Invalid S3 URL: must start with 's3://'")

        # Remove 's3://' prefix
        path = s3_url[5:]

        # Split into bucket and key
        parts = path.split("/", 1)
        if len(parts) != 2:
            raise ValueError("Invalid S3 URL: must contain bucket and key")

        bucket_name, key = parts
        if not bucket_name or not key:
            raise ValueError("Invalid S3 URL: empty bucket name or key")

        return bucket_name, key

    def read_file_to_memory(self, storage_url: str) -> BytesIO:
        """
        Read file from S3 into memory.

        Args:
            storage_url (str): S3 URL in format 's3://bucket-name/key'

        Returns:
            BytesIO: File contents in memory
        """
        try:
            # Parse S3 URL
            bucket_name, key = self._parse_s3_url(storage_url)
            logger.info(f"Reading file from S3 - Bucket: {bucket_name}, Key: {key}")

            # Create BytesIO object to store file contents
            file_obj = BytesIO()

            # Download file to memory
            self.s3_client.download_fileobj(Bucket=bucket_name, Key=key, Fileobj=file_obj)

            # Reset pointer to beginning of file
            file_obj.seek(0)
            logger.info("Successfully read file into memory")

            return file_obj

        except ClientError as e:
            logger.error(f"Failed to read file from S3: {str(e)}")
            raise ValueError(f"Failed to read file from S3: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error reading file: {str(e)}")
            raise


class GCPFileReader(CloudFileReader):
    """Google Cloud Storage implementation for reading files."""

    def __init__(self, region: Optional[str] = None):
        """
        Initialize GCP reader.

        Args:
            region (Optional[str]): GCP region name. Defaults to None.
        """
        # TODO: Initialize GCP client when needed
        pass

    def read_file_to_memory(self, storage_url: str) -> BytesIO:
        """Dummy implementation for GCP."""
        raise NotImplementedError("GCP implementation not yet available")


def get_cloud_reader(config: CloudConfig) -> CloudFileReader:
    """
    Get the appropriate cloud file reader based on configuration.

    Args:
        config (CloudConfig): Configuration for the cloud provider

    Returns:
        CloudFileReader: Instance of the appropriate cloud file reader

    Raises:
        ValueError: If the provider is not supported
    """
    readers = {
        "aws": AWSFileReader,
        "gcp": GCPFileReader,
        # Add more providers here
    }

    reader_class = readers.get(config.provider.lower())
    if not reader_class:
        raise ValueError(f"Unsupported cloud provider: {config.provider}")

    return reader_class(config.region)
