from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlparse, ParseResult
import boto3
from botocore.exceptions import ClientError
from dataclasses import dataclass
from urllib.parse import unquote
import logging

logger = logging.getLogger(__name__)


@dataclass
class CloudConfig:
    """Configuration for cloud provider."""

    provider: str
    region: Optional[str] = None


class AWSProvider:
    """AWS S3 implementation."""

    def __init__(self, config: CloudConfig):
        self.config = config
        self.s3_client = boto3.client("s3", region_name=config.region)

    def convert_to_storage_url(self, https_url: str) -> str:
        """Convert HTTPS URL to S3 URL."""
        try:
            parsed = urlparse(https_url)

            if not parsed.scheme == "https":
                raise ValueError("URL must be HTTPS")

            if not parsed.netloc:
                raise ValueError("Invalid URL: missing hostname")

            # Extract bucket name from hostname
            bucket_name = parsed.netloc.split(".")[0]

            if not bucket_name:
                raise ValueError("Invalid S3 URL: could not extract bucket name")

            # Get the path and handle URL encoding
            path = parsed.path.lstrip("/")
            path = path.replace("+", "%20")
            key = unquote(path)

            if not key:
                raise ValueError("Invalid S3 URL: empty key")

            s3_url = f"s3://{bucket_name}/{key}"
            logger.info(f"Successfully converted URL - From: {https_url}, To: {s3_url}")
            return s3_url

        except Exception as e:
            logger.error(f"Failed to convert URL '{https_url}': {str(e)}")
            raise ValueError(f"Failed to convert URL: {str(e)}")


class GCPProvider:
    """Google Cloud Storage implementation."""

    def __init__(self, config: CloudConfig):
        self.config = config
        # TODO: Initialize GCP client when needed
        pass

    def convert_to_storage_url(self, https_url: str) -> str:
        """Convert HTTPS URL to GCS URL."""
        try:
            parsed = urlparse(https_url)

            if not parsed.scheme == "https":
                raise ValueError("URL must be HTTPS")

            if not parsed.netloc:
                raise ValueError("Invalid URL: missing hostname")

            # Extract bucket name from hostname
            bucket_name = parsed.netloc.split(".")[0]

            if not bucket_name:
                raise ValueError("Invalid GCS URL: could not extract bucket name")

            # Get the path and handle URL encoding
            path = parsed.path.lstrip("/")
            path = path.replace("+", "%20")
            key = unquote(path)

            if not key:
                raise ValueError("Invalid GCS URL: empty key")

            gcs_url = f"gs://{bucket_name}/{key}"
            logger.info(f"Successfully converted URL - From: {https_url}, To: {gcs_url}")
            return gcs_url

        except Exception as e:
            logger.error(f"Failed to convert URL '{https_url}': {str(e)}")
            raise ValueError(f"Failed to convert URL: {str(e)}")


class CloudURIConverter:
    """Main class for converting HTTPS URLs to cloud storage URLs."""

    def __init__(self, config: CloudConfig):
        """
        Initialize the converter with cloud provider configuration.

        Args:
            config (CloudConfig): Configuration for the cloud provider
        """
        self.provider = self._get_provider(config)

    def _get_provider(self, config: CloudConfig):
        """Get the appropriate cloud provider implementation."""
        providers = {
            "aws": AWSProvider,
            "gcp": GCPProvider,
            # Add more providers here
        }

        provider_class = providers.get(config.provider.lower())
        if not provider_class:
            raise ValueError(f"Unsupported cloud provider: {config.provider}")

        return provider_class(config)

    def convert_url(self, https_url: str) -> str:
        """
        Convert HTTPS URL to cloud storage URL.

        Args:
            https_url (str): The HTTPS URL to convert

        Returns:
            str: The corresponding cloud storage URL
        """
        return self.provider.convert_to_storage_url(https_url)
