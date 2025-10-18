import os
from io import BytesIO

import docx
import fitz
from src.infra.storage.cloud_file_reader import (
    CloudConfig,
    CloudFileReader,
    get_cloud_reader,
)
from src.infra.storage.get_cloud_uri import CloudURIConverter


class DocumentReader:
    """Cloud-agnostic document reader service for multiple file formats."""

    def __init__(self, cloud_config: CloudConfig):
        """
        Initialize document reader with cloud configuration.

        Args:
            cloud_config (CloudConfig): Configuration for cloud provider
        """
        self.cloud_reader: CloudFileReader = get_cloud_reader(cloud_config)
        self.cloud_config = cloud_config

    def _read_docx(self, file_obj: BytesIO) -> str:
        """
        Read text from a DOCX file.

        Args:
            file_obj (BytesIO): DOCX file in memory

        Returns:
            str: Extracted text content
        """
        doc = docx.Document(file_obj)
        return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())

    def _read_pdf(self, file_obj: BytesIO) -> str:
        """
        Read text from a PDF file.

        Args:
            file_obj (BytesIO): PDF file in memory

        Returns:
            str: Extracted text content
        """
        # Open the PDF using PyMuPDF
        doc = fitz.open(stream=file_obj, filetype="pdf")
        # Extract text from all pages
        text = []
        for page in doc:
            text.append(page.get_text())
        doc.close()
        return "\n".join(text)

    def read_document(self, file_url: str) -> str:
        """
        Read text content from either a DOCX or PDF file stored in cloud storage.

        Args:
            file_url (str): URL to the file in cloud storage

        Returns:
            str: Extracted text content

        Raises:
            ValueError: If file extension is not supported or file cannot be read
        """
        # Convert URL to proper cloud storage URI
        storage_uri = CloudURIConverter(self.cloud_config).convert_url(file_url)

        # Get file extension
        ext = os.path.splitext(file_url)[1].lower()
        if ext not in [".docx", ".pdf"]:
            raise ValueError("Only .docx or .pdf files are supported")

        try:
            # Read file into memory
            file_obj = self.cloud_reader.read_file_to_memory(storage_uri)

            # Process based on file type
            if ext == ".docx":
                return self._read_docx(file_obj)
            else:  # ext == ".pdf"
                return self._read_pdf(file_obj)
        except Exception as e:
            raise ValueError(f"Failed to read document: {str(e)}") from e
