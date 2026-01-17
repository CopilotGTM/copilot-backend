from __future__ import annotations

import contextlib
import hashlib
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.knowledge_models import OrganisationDocument
from src.infra.database.sessions import acquire_org_session
from src.infra.utils.logger import get_logger

logger = get_logger("document_store")


class DocumentStore:
    """Service for storing and managing parsed documents."""

    def __init__(self, organisation_id: str):
        """
        Initialize the document store.

        Args:
            organisation_id: The organisation ID
        """
        self.organisation_id = organisation_id

    def store_documents(
        self,
        documents: list[dict[str, Any]] | list[OrganisationDocument],
        source_id: str,
        uploader_user_id: str | None = None,
    ) -> list[str]:
        """
        Store parsed documents in the database.

        Args:
            documents: List of parsed document data (dicts) or OrganisationDocument objects
            source_id: The source ID these documents belong to
            uploader_user_id: Optional user ID for attribution

        Returns:
            List of stored document IDs
        """
        stored_ids = []

        try:
            session_gen = acquire_org_session(self.organisation_id)
            session = next(session_gen)

            for doc_data in documents:
                try:
                    # Handle both OrganisationDocument objects and dict data
                    if isinstance(doc_data, OrganisationDocument):
                        # Already an OrganisationDocument - use it directly
                        doc = doc_data
                        computed_hash = doc.content_hash or self._generate_content_hash(
                            doc.content or ""
                        )

                        # Check for duplicates
                        existing = (
                            session.query(OrganisationDocument)
                            .filter_by(content_hash=computed_hash)
                            .first()
                        )
                        if existing:
                            logger.debug(
                                f"Skipping duplicate document with content_hash={computed_hash}"
                            )
                            continue

                        # Set content_hash if not already set
                        if not doc.content_hash:
                            doc.content_hash = computed_hash

                        session.add(doc)
                        stored_ids.append(str(doc.document_id))
                        logger.debug(
                            f"Added document {doc.document_id} to session with content_hash={computed_hash}"
                        )
                    else:
                        # Dict data - create OrganisationDocument
                        uid = uploader_user_id
                        if not uid:
                            uid = doc_data.get("uploader_user_id") or doc_data.get(
                                "meta_data", {}
                            ).get("user_id")

                        computed_hash = doc_data.get(
                            "content_hash",
                            self._generate_content_hash(doc_data.get("content", "")),
                        )

                        # Check if content_hash already exists to avoid UniqueViolation
                        existing = (
                            session.query(OrganisationDocument)
                            .filter_by(content_hash=computed_hash)
                            .first()
                        )
                        if existing:
                            logger.debug(
                                f"Skipping duplicate document with content_hash={computed_hash}"
                            )
                            continue

                        doc = OrganisationDocument(
                            source_id=UUID(source_id),
                            uploader_user_id=UUID(uid) if uid else None,
                            name=doc_data.get("name", "Untitled"),
                            content=doc_data.get("content", ""),
                            meta_data=doc_data.get("meta_data", {}),
                            filters=doc_data.get("filters", {}),
                            embedding=doc_data.get("embedding", []),
                            usage=doc_data.get("usage", {}),
                            content_hash=computed_hash,
                        )

                        session.add(doc)
                        stored_ids.append(str(doc.document_id))
                        logger.debug(
                            f"Added document {doc.document_id} to session with content_hash={computed_hash}"
                        )

                except Exception as e:
                    logger.error(f"Error creating document: {str(e)}")
                    continue

            # Commit all documents
            session.commit()
            logger.info(f"Successfully stored {len(stored_ids)} documents for source {source_id}")

        except Exception as e:
            logger.error(f"Error storing documents: {str(e)}")
            with contextlib.suppress(Exception):
                session.rollback()
        finally:
            with contextlib.suppress(Exception):
                session.close()
            with contextlib.suppress(Exception):
                session_gen.close()

        return stored_ids

    def update_document(self, document_id: str, updates: dict[str, Any]) -> bool:
        """
        Update an existing document.

        Args:
            document_id: The document ID to update
            updates: Dictionary of fields to update

        Returns:
            True if update was successful, False otherwise
        """
        try:
            session_gen = acquire_org_session(self.organisation_id)
            session = next(session_gen)

            doc = session.query(OrganisationDocument).filter_by(document_id=document_id).first()
            if not doc:
                logger.warning(f"Document {document_id} not found for update")
                return False

            # Update fields
            for field, value in updates.items():
                if hasattr(doc, field):
                    setattr(doc, field, value)

            # Update content hash if content was changed
            if "content" in updates:
                doc.content_hash = self._generate_content_hash(updates["content"])

            session.commit()
            logger.info(f"Successfully updated document {document_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating document {document_id}: {str(e)}")
            with contextlib.suppress(Exception):
                session.rollback()
            return False
        finally:
            with contextlib.suppress(Exception):
                session.close()
            with contextlib.suppress(Exception):
                session_gen.close()

    def delete_documents_by_source(self, source_id: str) -> int:
        """
        Delete all documents belonging to a source.

        Args:
            source_id: The source ID

        Returns:
            Number of documents deleted
        """
        try:
            session_gen = acquire_org_session(self.organisation_id)
            session = next(session_gen)

            deleted_count = (
                session.query(OrganisationDocument).filter_by(source_id=UUID(source_id)).delete()
            )
            session.commit()

            logger.info(f"Deleted {deleted_count} documents for source {source_id}")
            return deleted_count

        except Exception as e:
            logger.error(f"Error deleting documents for source {source_id}: {str(e)}")
            with contextlib.suppress(Exception):
                session.rollback()
            return 0
        finally:
            with contextlib.suppress(Exception):
                session.close()
            with contextlib.suppress(Exception):
                session_gen.close()

    def get_documents_by_source(self, source_id: str) -> list[OrganisationDocument]:
        """
        Get all documents belonging to a source.

        Args:
            source_id: The source ID

        Returns:
            List of OrganisationDocument instances
        """
        try:
            session_gen = acquire_org_session(self.organisation_id)
            session = next(session_gen)

            documents = (
                session.query(OrganisationDocument).filter_by(source_id=UUID(source_id)).all()
            )
            logger.info(f"Retrieved {len(documents)} documents for source {source_id}")
            return documents

        except Exception as e:
            logger.error(f"Error retrieving documents for source {source_id}: {str(e)}")
            return []
        finally:
            with contextlib.suppress(Exception):
                session.close()
            with contextlib.suppress(Exception):
                session_gen.close()

    def _generate_content_hash(self, content: str) -> str:
        """Generate SHA256 hash for content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
