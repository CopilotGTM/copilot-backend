from src.infra.utils.logger import get_logger

# import boto3
# from botocore.exceptions import ClientError

logger = get_logger()


class GeneratePresignedURL:
    def __init__(self):
        # Initialize AWS S3 client
        # try:
        #     self.s3_client = boto3.client("s3")
        #     logger.info("Successfully initialized S3 client")
        # except Exception as e:
        #     logger.error(f"Failed to initialize S3 client: {str(e)}")
        self.s3_client = None

    def generate_presigned_url(
        self,
        cloud_name: str,
        container_name: str,
        blob_path: str,
        file_type: str,
        expire_seconds: int = 300,
    ) -> str | None:
        """
        Generate a presigned URL for file upload.
        Currently supports AWS S3. Structure prepared for future cloud providers.

        Args:
            cloud_name: Cloud provider name (currently only 'aws' is supported)
            container_name: Bucket name for AWS S3
            blob_path: File path within the bucket
            expire_seconds: URL expiration time in seconds
        """
        cloud_name = cloud_name.lower()

        if cloud_name == "aws":
            return self._generate_s3_url(container_name, blob_path, file_type, expire_seconds)
        else:
            logger.error(f"Unsupported cloud provider: {cloud_name}")
            return None

    def _generate_s3_url(
        self, bucket_name: str, file_path: str, file_type, expire_seconds: int
    ) -> str | None:
        """Generate S3 presigned URL using boto3"""
        # if not self.s3_client:
        #     logger.error("S3 client not initialized")
        #     return None

        # try:
        #     logger.info(f"Generating S3 presigned URL for {file_path} in bucket {bucket_name}")
        #     response = self.s3_client.generate_presigned_url(
        #         "put_object",
        #         Params={"Bucket": bucket_name, "Key": file_path, "ContentType": file_type},
        #         ExpiresIn=expire_seconds,
        #     )
        #     logger.info(f"Successfully generated presigned URL for {file_path}")
        #     return response

        # except ClientError as e:
        #     logger.error(f"Error generating S3 presigned URL: {str(e)}")
        #     return None
        # except Exception as e:
        #     logger.error(f"Unexpected error generating S3 presigned URL: {str(e)}")
        return None
