from typing import List, Dict, Any, Optional, Tuple
from agno.tools import tool
from agno.embedder.azure_openai import AzureOpenAIEmbedder
from sqlmodel import select
from models.embeddings_db_models import OrganisationDocument
from core.database import get_master_session
import asyncio


class SolutionDeckSearchTool:
    """
    A tool for searching through solution decks using Retrieval Augmented Generation (RAG).
    
    This tool uses vector embeddings to find the most relevant solution decks based on
    semantic similarity to the search query.
    """
    
    def __init__(self) -> None:
        """Initialize the search tool with database session and embedder."""
        self.master_session = next(get_master_session())
        self.embedder = AzureOpenAIEmbedder()
    
    def validate_filters(self, filters: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """
        Validate and process filters for the search query.
        
        Args:
            filters: Dictionary of filter parameters
            
        Returns:
            Tuple containing:
            - Dictionary of valid filters
            - List of invalid filter keys
        """
        if filters is None:
            return {}, []
            
        valid_filters = {}
        invalid_keys = []
        
        # Add any filter validation logic here
        # For now, we'll just pass through any filters
        valid_filters = filters.copy()
        
        return valid_filters, invalid_keys
    
    def format_document(self, doc: OrganisationDocument):
        """
        Format a document into a structured SlideData object.
        
        Args:
            doc: The organisation document containing slide information
            
        Returns:
            SlideData object containing formatted slide summary and metadata
        """
        metadata = doc.meta_data.get('slide_data', {})
        
        return {
            "slide_summary":doc.content,
            "slide_json":metadata
        }
    
    @tool(
        name="search_solution_decks",
        description="Search through solution decks to find relevant content based on semantic similarity. Use this when you need to find previous solution decks that might be relevant to the current topic.",
        show_result=True
    )
    async def search(self, query: str, limit: Optional[int] = 4) -> str:
        """
        Search for solution decks that are semantically similar to the query.
        
        Args:
            query: The search query to find relevant solution decks
            limit: Maximum number of results to return (default: 2)
            
        Returns:
            A formatted string containing the search results
            
        Example:
            >>> tool = SolutionDeckSearchTool()
            >>> results = await tool.search("cloud migration strategy")
        """
        # Generate embedding for semantic search
        query_embedding = self.embedder.get_embedding(query)
        
        # Query database for solution decks using cosine similarity
        stmt = (
            select(OrganisationDocument)
            .where(OrganisationDocument.filters['is_solution_deck'].astext == 'true')
            .order_by(OrganisationDocument.embedding.cosine_distance(query_embedding))
            .limit(limit)
        )
        
        # Execute query and format results
        results = self.master_session.exec(stmt).all()
        formatted_results = [self.format_document(doc) for doc in results]
        
        return formatted_results

# async def main() -> None:
#     """Example usage of the SolutionDeckSearchTool."""
#     # Initialize the search tool
#     search_tool = SolutionDeckSearchTool()
    
#     # Example search query
#     test_query = "Title slide Xeno partner"
    
#     try:
#         results = await search_tool.search(test_query)
#         print(f"{results}")

#     except Exception as e:
#         print(f"Error during search: {str(e)}")

# if __name__ == "__main__":
#     asyncio.run(main())