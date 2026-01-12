"""
Unit tests for research tools used by GeneralAgent.

These tests cover the free research tools that don't require API keys:
- Wikipedia search
- YFinance (stock info and company financials)
- Web crawler (Crawl4AI)
- Arxiv search
- News article extraction (Newspaper4k)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.agents.tools.research import (
    arxiv_search,
    extract_news_article,
    get_company_financials,
    get_stock_info,
    wikipedia_search,
)

# Note: crawl_webpage is imported inside test methods to avoid import-time issues


class TestWikipediaSearch:
    """Test suite for Wikipedia search tool."""

    def test_empty_query_returns_empty_list(self):
        """Test that empty query returns empty list."""
        result = wikipedia_search("")
        assert result == []

    def test_whitespace_query_returns_empty_list(self):
        """Test that whitespace-only query returns empty list."""
        result = wikipedia_search("   ")
        assert result == []

    def test_limit_clamped_to_max(self):
        """Test that limit is clamped to maximum of 10."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            mock_wiki.search.return_value = []
            wikipedia_search("test", limit=100)
            mock_wiki.search.assert_called_once_with("test", results=10)

    def test_limit_clamped_to_min(self):
        """Test that limit is clamped to minimum of 1."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            mock_wiki.search.return_value = []
            wikipedia_search("test", limit=0)
            mock_wiki.search.assert_called_once_with("test", results=1)

    def test_successful_search(self):
        """Test successful Wikipedia search returns formatted results."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            # Mock search results
            mock_wiki.search.return_value = ["Microsoft"]

            # Mock page and summary
            mock_page = MagicMock()
            mock_page.title = "Microsoft"
            mock_page.url = "https://en.wikipedia.org/wiki/Microsoft"
            mock_wiki.page.return_value = mock_page
            mock_wiki.summary.return_value = "Microsoft Corporation is an American multinational."

            result = wikipedia_search("Microsoft", limit=1)

            assert len(result) == 1
            assert result[0]["label"] == "Microsoft"
            assert result[0]["url"] == "https://en.wikipedia.org/wiki/Microsoft"
            assert "Microsoft Corporation" in result[0]["snippet"]

    def test_handles_page_not_found(self):
        """Test that PageError is handled gracefully."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            mock_wiki.search.return_value = ["NonExistentPage"]
            mock_wiki.exceptions.PageError = Exception
            mock_wiki.exceptions.DisambiguationError = Exception
            mock_wiki.page.side_effect = Exception("Page not found")

            result = wikipedia_search("NonExistentPage")
            assert result == []

    def test_handles_disambiguation(self):
        """Test that DisambiguationError is handled by taking first option."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            # Create proper exception classes
            class DisambiguationError(Exception):
                def __init__(self, options):
                    self.options = options

            class PageError(Exception):
                pass

            mock_wiki.exceptions.DisambiguationError = DisambiguationError
            mock_wiki.exceptions.PageError = PageError
            mock_wiki.search.return_value = ["Python"]

            # First call raises disambiguation
            disambig_error = DisambiguationError(["Python (programming)", "Python (snake)"])

            mock_page = MagicMock()
            mock_page.title = "Python (programming language)"
            mock_page.url = "https://en.wikipedia.org/wiki/Python_(programming_language)"

            # First page call raises, second succeeds
            mock_wiki.page.side_effect = [disambig_error, mock_page]
            mock_wiki.summary.return_value = "Python is a programming language."

            result = wikipedia_search("Python", limit=1)

            assert len(result) == 1
            assert "Python" in result[0]["label"]

    def test_truncates_long_snippets(self):
        """Test that long snippets are truncated to 500 chars."""
        with patch("src.agents.tools.research.wikipedia_search.wikipedia") as mock_wiki:
            mock_wiki.search.return_value = ["Test"]

            mock_page = MagicMock()
            mock_page.title = "Test"
            mock_page.url = "https://example.com"
            mock_wiki.page.return_value = mock_page
            mock_wiki.summary.return_value = "A" * 600  # 600 chars

            result = wikipedia_search("Test", limit=1)

            assert len(result) == 1
            assert len(result[0]["snippet"]) == 503  # 500 + "..."
            assert result[0]["snippet"].endswith("...")


class TestYFinanceStockInfo:
    """Test suite for YFinance stock info tool."""

    def test_empty_symbol_returns_empty_dict(self):
        """Test that empty symbol returns empty dict."""
        result = get_stock_info("")
        assert result == {}

    def test_whitespace_symbol_returns_empty_dict(self):
        """Test that whitespace-only symbol returns empty dict."""
        result = get_stock_info("   ")
        assert result == {}

    def test_symbol_is_uppercased(self):
        """Test that symbol is normalized to uppercase."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {"regularMarketPrice": 150.0}
            mock_yf.Ticker.return_value = mock_ticker

            get_stock_info("aapl")
            mock_yf.Ticker.assert_called_once_with("AAPL")

    def test_successful_stock_info(self):
        """Test successful stock info retrieval."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {
                "regularMarketPrice": 175.50,
                "longName": "Apple Inc.",
                "currency": "USD",
                "marketCap": 2800000000000,
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "website": "https://www.apple.com",
                "fullTimeEmployees": 164000,
                "country": "United States",
                "city": "Cupertino",
                "longBusinessSummary": "Apple Inc. designs, manufactures, and markets smartphones.",
            }
            mock_yf.Ticker.return_value = mock_ticker

            result = get_stock_info("AAPL")

            assert result["symbol"] == "AAPL"
            assert result["company_name"] == "Apple Inc."
            assert result["current_price"] == 175.50
            assert result["market_cap"] == 2800000000000
            assert result["sector"] == "Technology"

    def test_returns_empty_when_no_price(self):
        """Test that empty dict is returned when no price data."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {"regularMarketPrice": None}
            mock_yf.Ticker.return_value = mock_ticker

            result = get_stock_info("INVALID")
            assert result == {}

    def test_includes_price_change_when_available(self):
        """Test that price change info is included when available."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {
                "regularMarketPrice": 175.50,
                "longName": "Apple Inc.",
                "regularMarketChange": 2.50,
                "regularMarketChangePercent": 1.44,
            }
            mock_yf.Ticker.return_value = mock_ticker

            result = get_stock_info("AAPL")

            assert result["price_change"] == 2.50
            assert result["price_change_percent"] == 1.44

    def test_includes_52_week_range_when_available(self):
        """Test that 52-week range is included when available."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {
                "regularMarketPrice": 175.50,
                "longName": "Apple Inc.",
                "fiftyTwoWeekHigh": 199.62,
                "fiftyTwoWeekLow": 124.17,
            }
            mock_yf.Ticker.return_value = mock_ticker

            result = get_stock_info("AAPL")

            assert result["52_week_high"] == 199.62
            assert result["52_week_low"] == 124.17

    def test_handles_api_exception(self):
        """Test that API exceptions are handled gracefully."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_yf.Ticker.side_effect = Exception("API Error")

            result = get_stock_info("AAPL")
            assert result == {}


class TestYFinanceCompanyFinancials:
    """Test suite for YFinance company financials tool."""

    def test_empty_symbol_returns_empty_dict(self):
        """Test that empty symbol returns empty dict."""
        result = get_company_financials("")
        assert result == {}

    def test_successful_financials(self):
        """Test successful financial data retrieval."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {
                "longName": "Apple Inc.",
                "marketCap": 2800000000000,
                "enterpriseValue": 2900000000000,
                "trailingPE": 28.5,
                "forwardPE": 25.2,
                "priceToBook": 47.3,
                "totalRevenue": 394000000000,
                "revenueGrowth": 0.08,
                "grossMargins": 0.438,
                "operatingMargins": 0.302,
                "profitMargins": 0.253,
                "returnOnEquity": 1.47,
                "dividendYield": 0.005,
                "recommendationKey": "buy",
                "targetMeanPrice": 195.0,
                "numberOfAnalystOpinions": 42,
            }
            mock_yf.Ticker.return_value = mock_ticker

            result = get_company_financials("AAPL")

            assert result["symbol"] == "AAPL"
            assert result["company_name"] == "Apple Inc."
            assert result["valuation"]["market_cap"] == 2800000000000
            assert result["valuation"]["pe_ratio_trailing"] == 28.5
            assert result["financials"]["revenue_growth"] == "8.0%"
            assert result["financials"]["gross_margin"] == "43.8%"
            assert result["dividend_yield"] == "0.50%"
            assert result["analyst_recommendation"] == "buy"
            assert result["target_price"] == 195.0

    def test_returns_empty_when_no_info(self):
        """Test that empty dict is returned when no info."""
        with patch("src.agents.tools.research.yfinance_tools.yf") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.info = {}
            mock_yf.Ticker.return_value = mock_ticker

            result = get_company_financials("INVALID")
            # Empty info returns empty dict
            assert result == {}


class TestArxivSearch:
    """Test suite for Arxiv search tool."""

    def test_empty_query_returns_empty_list(self):
        """Test that empty query returns empty list."""
        result = arxiv_search("")
        assert result == []

    def test_max_results_clamped(self):
        """Test that max_results is clamped to 20."""
        with patch("src.agents.tools.research.arxiv_search.arxiv") as mock_arxiv:
            mock_client = MagicMock()
            mock_client.results.return_value = iter([])
            mock_arxiv.Client.return_value = mock_client
            mock_arxiv.Search.return_value = MagicMock()
            mock_arxiv.SortCriterion.Relevance = "relevance"

            arxiv_search("machine learning", max_results=100)

            # Check that Search was called with max_results=20
            call_kwargs = mock_arxiv.Search.call_args[1]
            assert call_kwargs["max_results"] == 20

    def test_successful_search(self):
        """Test successful Arxiv search."""
        with patch("src.agents.tools.research.arxiv_search.arxiv") as mock_arxiv:
            from datetime import datetime

            # Create mock paper
            mock_author = MagicMock()
            mock_author.name = "John Doe"

            mock_paper = MagicMock()
            mock_paper.title = "Deep Learning for NLP"
            mock_paper.entry_id = "https://arxiv.org/abs/2301.00001"
            mock_paper.pdf_url = "https://arxiv.org/pdf/2301.00001"
            mock_paper.authors = [mock_author]
            mock_paper.published = datetime(2023, 1, 1)
            mock_paper.categories = ["cs.CL", "cs.AI"]
            mock_paper.summary = "This paper presents a novel approach."

            mock_client = MagicMock()
            mock_client.results.return_value = iter([mock_paper])
            mock_arxiv.Client.return_value = mock_client
            mock_arxiv.Search.return_value = MagicMock()
            mock_arxiv.SortCriterion.Relevance = "relevance"

            result = arxiv_search("deep learning NLP", max_results=1)

            assert len(result) == 1
            assert result[0]["title"] == "Deep Learning for NLP"
            assert result[0]["authors"] == ["John Doe"]
            assert result[0]["published"] == "2023-01-01"

    def test_handles_many_authors(self):
        """Test that author list is limited to first 3 + count."""
        with patch("src.agents.tools.research.arxiv_search.arxiv") as mock_arxiv:
            from datetime import datetime

            # Create 5 mock authors
            mock_authors = [MagicMock(name=f"Author {i}") for i in range(5)]
            for i, author in enumerate(mock_authors):
                author.name = f"Author {i}"

            mock_paper = MagicMock()
            mock_paper.title = "Paper with many authors"
            mock_paper.entry_id = "https://arxiv.org/abs/2301.00001"
            mock_paper.pdf_url = "https://arxiv.org/pdf/2301.00001"
            mock_paper.authors = mock_authors
            mock_paper.published = datetime(2023, 1, 1)
            mock_paper.categories = ["cs.AI"]
            mock_paper.summary = "Summary"

            mock_client = MagicMock()
            mock_client.results.return_value = iter([mock_paper])
            mock_arxiv.Client.return_value = mock_client
            mock_arxiv.Search.return_value = MagicMock()
            mock_arxiv.SortCriterion.Relevance = "relevance"

            result = arxiv_search("test", max_results=1)

            assert len(result[0]["authors"]) == 4  # 3 names + "+ 2 more"
            assert "+ 2 more" in result[0]["authors"][-1]


class TestNewsArticleExtraction:
    """Test suite for news article extraction tool."""

    def test_empty_url_returns_empty_dict(self):
        """Test that empty URL returns empty dict."""
        result = extract_news_article("")
        assert result == {}

    def test_invalid_url_scheme_returns_empty_dict(self):
        """Test that invalid URL scheme returns empty dict."""
        result = extract_news_article("ftp://example.com/article")
        assert result == {}

    def test_url_without_domain_returns_empty_dict(self):
        """Test that URL without domain returns empty dict."""
        result = extract_news_article("https://")
        assert result == {}

    def test_adds_https_if_missing(self):
        """Test that https:// is added if scheme is missing."""
        with patch("newspaper.Article") as mock_article_class:
            mock_article = MagicMock()
            mock_article.text = "Article content"
            mock_article.title = "Article Title"
            mock_article.authors = []
            mock_article.publish_date = None
            mock_article.top_image = None
            mock_article.keywords = []
            mock_article.meta_description = None
            mock_article.summary = None
            mock_article_class.return_value = mock_article

            extract_news_article("example.com/article")

            mock_article_class.assert_called_once_with("https://example.com/article")

    def test_successful_extraction(self):
        """Test successful article extraction."""
        with patch("newspaper.Article") as mock_article_class:
            from datetime import datetime

            mock_article = MagicMock()
            mock_article.text = "Full article content here."
            mock_article.title = "Breaking News: Important Event"
            mock_article.authors = ["Jane Smith", "John Doe"]
            mock_article.publish_date = datetime(2024, 1, 15)
            mock_article.top_image = "https://example.com/image.jpg"
            mock_article.keywords = ["news", "event", "breaking"]
            mock_article.meta_description = "A summary of the event."
            mock_article.summary = "This article covers an important event."
            mock_article_class.return_value = mock_article

            result = extract_news_article("https://news.example.com/article")

            assert result["title"] == "Breaking News: Important Event"
            assert result["content"] == "Full article content here."
            assert result["authors"] == ["Jane Smith", "John Doe"]
            assert result["published_date"] == "2024-01-15"
            assert result["keywords"] == ["news", "event", "breaking"]

    def test_truncates_long_content(self):
        """Test that long content is truncated to 5000 chars."""
        with patch("newspaper.Article") as mock_article_class:
            mock_article = MagicMock()
            mock_article.text = "A" * 6000  # 6000 chars
            mock_article.title = "Title"
            mock_article.authors = []
            mock_article.publish_date = None
            mock_article.top_image = None
            mock_article.keywords = []
            mock_article.meta_description = None
            mock_article.summary = None
            mock_article_class.return_value = mock_article

            result = extract_news_article("https://example.com/article")

            assert len(result["content"]) == 5003  # 5000 + "..."
            assert result["content"].endswith("...")

    def test_handles_missing_optional_fields(self):
        """Test graceful handling of missing optional fields."""
        with patch("newspaper.Article") as mock_article_class:
            mock_article = MagicMock()
            mock_article.text = "Content"
            mock_article.title = "Title"
            mock_article.authors = []
            mock_article.publish_date = None
            mock_article.top_image = None
            mock_article.keywords = []
            mock_article.meta_description = None
            mock_article.summary = None
            mock_article_class.return_value = mock_article

            result = extract_news_article("https://example.com/article")

            assert "authors" not in result  # Empty list not included
            assert "published_date" not in result  # None not included
            assert result["title"] == "Title"
            assert result["content"] == "Content"


class TestWebCrawler:
    """Test suite for web crawler tool."""

    @pytest.mark.asyncio
    async def test_empty_url_returns_empty_dict(self):
        """Test that empty URL returns empty dict."""
        from src.agents.tools.research import crawl_webpage

        result = await crawl_webpage("")
        assert result == {}

    @pytest.mark.asyncio
    async def test_invalid_url_scheme_returns_empty_dict(self):
        """Test that invalid URL scheme returns empty dict."""
        from src.agents.tools.research import crawl_webpage

        result = await crawl_webpage("ftp://example.com")
        assert result == {}

    @pytest.mark.asyncio
    async def test_adds_https_if_missing(self):
        """Test that https:// is added if scheme is missing."""
        from src.agents.tools.research import crawl_webpage

        with patch("crawl4ai.AsyncWebCrawler") as mock_crawler_class:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.markdown = "Page content"
            mock_result.cleaned_html = None
            mock_result.metadata = {"title": "Page Title"}
            mock_result.links = {}

            mock_crawler = AsyncMock()
            mock_crawler.arun.return_value = mock_result
            mock_crawler.__aenter__.return_value = mock_crawler
            mock_crawler.__aexit__.return_value = None
            mock_crawler_class.return_value = mock_crawler

            await crawl_webpage("example.com")

            mock_crawler.arun.assert_called_once_with(url="https://example.com")

    @pytest.mark.asyncio
    async def test_successful_crawl(self):
        """Test successful web page crawl."""
        from src.agents.tools.research import crawl_webpage

        with patch("crawl4ai.AsyncWebCrawler") as mock_crawler_class:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.markdown = "# Heading\n\nPage content here."
            mock_result.cleaned_html = None
            mock_result.metadata = {
                "title": "Example Page",
                "description": "A sample page description.",
                "author": "John Doe",
            }
            mock_result.links = {
                "internal": ["https://example.com/page1", "https://example.com/page2"],
                "external": ["https://other.com/link"],
            }

            mock_crawler = AsyncMock()
            mock_crawler.arun.return_value = mock_result
            mock_crawler.__aenter__.return_value = mock_crawler
            mock_crawler.__aexit__.return_value = None
            mock_crawler_class.return_value = mock_crawler

            result = await crawl_webpage("https://example.com")

            assert result["url"] == "https://example.com"
            assert result["title"] == "Example Page"
            assert "# Heading" in result["content"]
            assert result["description"] == "A sample page description."
            assert result["author"] == "John Doe"
            assert len(result["links"]["internal"]) == 2

    @pytest.mark.asyncio
    async def test_truncates_long_content(self):
        """Test that long content is truncated."""
        from src.agents.tools.research import crawl_webpage

        with patch("crawl4ai.AsyncWebCrawler") as mock_crawler_class:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.markdown = "A" * 10000  # 10000 chars
            mock_result.cleaned_html = None
            mock_result.metadata = {"title": "Long Page"}
            mock_result.links = {}

            mock_crawler = AsyncMock()
            mock_crawler.arun.return_value = mock_result
            mock_crawler.__aenter__.return_value = mock_crawler
            mock_crawler.__aexit__.return_value = None
            mock_crawler_class.return_value = mock_crawler

            result = await crawl_webpage("https://example.com", max_content_length=5000)

            assert len(result["content"]) == 5003  # 5000 + "..."
            assert result["content"].endswith("...")

    @pytest.mark.asyncio
    async def test_handles_crawl_failure(self):
        """Test graceful handling of crawl failure."""
        from src.agents.tools.research import crawl_webpage

        with patch("crawl4ai.AsyncWebCrawler") as mock_crawler_class:
            mock_result = MagicMock()
            mock_result.success = False

            mock_crawler = AsyncMock()
            mock_crawler.arun.return_value = mock_result
            mock_crawler.__aenter__.return_value = mock_crawler
            mock_crawler.__aexit__.return_value = None
            mock_crawler_class.return_value = mock_crawler

            result = await crawl_webpage("https://blocked-site.com")
            assert result == {}


class TestToolRegistry:
    """Test suite for research tools in ToolRegistry."""

    def test_research_tools_in_general_agent(self):
        """Test that research tools are available for GeneralAgent."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        general_agent_tools = AGENT_TOOLS["GeneralAgent"]

        assert ToolName.SEARCH_WIKIPEDIA in general_agent_tools
        assert ToolName.GET_STOCK_INFO in general_agent_tools
        assert ToolName.GET_COMPANY_FINANCIALS in general_agent_tools
        assert ToolName.CRAWL_WEBPAGE in general_agent_tools
        assert ToolName.SEARCH_ARXIV in general_agent_tools
        assert ToolName.EXTRACT_NEWS_ARTICLE in general_agent_tools

    def test_research_tools_categorized_as_search(self):
        """Test that research tools are categorized correctly."""
        from src.agents.tools.registry import TOOL_CATEGORIES, ToolCategory, ToolName

        assert TOOL_CATEGORIES[ToolName.SEARCH_WIKIPEDIA] == ToolCategory.SEARCH
        assert TOOL_CATEGORIES[ToolName.GET_STOCK_INFO] == ToolCategory.SEARCH
        assert TOOL_CATEGORIES[ToolName.GET_COMPANY_FINANCIALS] == ToolCategory.SEARCH
        assert TOOL_CATEGORIES[ToolName.CRAWL_WEBPAGE] == ToolCategory.SEARCH
        assert TOOL_CATEGORIES[ToolName.SEARCH_ARXIV] == ToolCategory.SEARCH
        assert TOOL_CATEGORIES[ToolName.EXTRACT_NEWS_ARTICLE] == ToolCategory.SEARCH
