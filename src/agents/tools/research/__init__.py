"""Research tools package for agent capabilities.

Provides research tools for agent workflows:
- Wikipedia search for background research
- YFinance for stock and financial data
- Web crawler for page content extraction
- Arxiv for academic paper research
- News article extraction for company/industry news
"""

from .arxiv_search import arxiv_search, create_search_arxiv
from .news_article import create_extract_news_article, extract_news_article
from .research_linkedin_profile import (
    create_research_linkedin_profile,
    research_linkedin_profile,
)
from .web_crawler import crawl_webpage, create_crawl_webpage
from .wikipedia_search import create_search_wikipedia, wikipedia_search
from .yfinance_tools import (
    create_get_company_financials,
    create_get_stock_info,
    get_company_financials,
    get_stock_info,
)

__all__ = [
    # Wikipedia
    "wikipedia_search",
    "create_search_wikipedia",
    # YFinance
    "get_stock_info",
    "get_company_financials",
    "create_get_stock_info",
    "create_get_company_financials",
    # Web Crawler
    "crawl_webpage",
    "create_crawl_webpage",
    # Arxiv
    "arxiv_search",
    "create_search_arxiv",
    # News Article
    "extract_news_article",
    "create_extract_news_article",
    # LinkedIn Research (for general chat)
    "research_linkedin_profile",
    "create_research_linkedin_profile",
]
