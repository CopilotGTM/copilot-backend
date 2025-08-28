from __future__ import annotations

import os
from typing import List, Optional
from functools import lru_cache

from agno.embedder.azure_openai import AzureOpenAIEmbedder
from src.infra.utils.logger import get_logger

logger = get_logger("embedding_service")


class EmbeddingService:
    """Service for generating embeddings for document content."""
    
    def __init__(self):
        """Initialize the embedding service with Azure OpenAI embedder."""
        self._embedder: Optional[AzureOpenAIEmbedder] = None
    
    @property
    def embedder(self) -> AzureOpenAIEmbedder:
        """Get or create the embedder instance."""
        if self._embedder is None:
            try:
                self._embedder = AzureOpenAIEmbedder()
                logger.info("Initialized Azure OpenAI embedder")
            except Exception as e:
                logger.error(f"Failed to initialize Azure OpenAI embedder: {str(e)}")
                raise
        return self._embedder
    
    def get_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for the given text.
        
        Args:
            text: The text to generate embedding for
            
        Returns:
            List of floats representing the embedding vector
        """
        try:
            if not text or not text.strip():
                logger.warning("Empty text provided for embedding")
                return [0.0] * 1536  # Return zero vector for empty text
            
            # Truncate text if too long (Azure OpenAI has token limits)
            max_length = 8000  # Conservative limit for token count
            if len(text) > max_length:
                text = text[:max_length]
                logger.warning(f"Text truncated to {max_length} characters for embedding")
            
            embedding = self.embedder.get_embedding(text)
            logger.debug(f"Generated embedding of dimension {len(embedding)} for text of length {len(text)}")
            return embedding
            
        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}")
            # Return zero vector as fallback
            return [0.0] * 1536
    
    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.
        
        Args:
            texts: List of texts to generate embeddings for
            
        Returns:
            List of embedding vectors
        """
        embeddings = []
        for text in texts:
            embedding = self.get_embedding(text)
            embeddings.append(embedding)
        return embeddings


# Global embedding service instance
@lru_cache(maxsize=1)
def get_embedding_service() -> EmbeddingService:
    """Get the global embedding service instance."""
    return EmbeddingService()
