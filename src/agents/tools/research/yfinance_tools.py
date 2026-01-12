"""YFinance tools for financial intelligence.

Provides stock and company financial data without requiring API keys.
Useful for researching public companies, understanding their financial health,
and preparing for sales conversations with financial context.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import yfinance as yf  # type: ignore[import-untyped]

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def get_stock_info(
    symbol: str,
) -> dict[str, Any]:
    """Get stock price and company profile information.

    Args:
        symbol: Stock ticker symbol (e.g., "AAPL", "MSFT", "GOOGL")

    Returns:
        Dictionary with stock and company information, or empty dict on failure.
    """
    trimmed_symbol = (symbol or "").strip().upper()
    if not trimmed_symbol:
        logger.debug("GetStockInfo skipped: empty symbol provided")
        return {}

    try:
        ticker = yf.Ticker(trimmed_symbol)
        info = ticker.info

        if not info or info.get("regularMarketPrice") is None:
            logger.info("GetStockInfo returned no data for symbol: '%s'", trimmed_symbol)
            return {}

        # Extract relevant fields with safe defaults
        result: dict[str, Any] = {
            "symbol": trimmed_symbol,
            "company_name": info.get("longName") or info.get("shortName") or trimmed_symbol,
            "current_price": info.get("regularMarketPrice"),
            "currency": info.get("currency", "USD"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "website": info.get("website"),
            "employees": info.get("fullTimeEmployees"),
            "country": info.get("country"),
            "city": info.get("city"),
            "description": (
                info.get("longBusinessSummary", "")[:500] + "..."
                if len(info.get("longBusinessSummary", "")) > 500
                else info.get("longBusinessSummary")
            ),
        }

        # Add price change info if available
        if info.get("regularMarketChange") is not None:
            result["price_change"] = info.get("regularMarketChange")
            result["price_change_percent"] = info.get("regularMarketChangePercent")

        # Add 52-week range if available
        if info.get("fiftyTwoWeekHigh") is not None:
            result["52_week_high"] = info.get("fiftyTwoWeekHigh")
            result["52_week_low"] = info.get("fiftyTwoWeekLow")

        # Remove None values for cleaner output
        result = {k: v for k, v in result.items() if v is not None}

        return result

    except Exception as exc:
        logger.warning("GetStockInfo failed for '%s': %s", trimmed_symbol, exc)
        return {}


def get_company_financials(
    symbol: str,
) -> dict[str, Any]:
    """Get financial metrics and analyst recommendations for a company.

    Args:
        symbol: Stock ticker symbol (e.g., "AAPL", "MSFT", "GOOGL")

    Returns:
        Dictionary with financial metrics, or empty dict on failure.
    """
    trimmed_symbol = (symbol or "").strip().upper()
    if not trimmed_symbol:
        logger.debug("GetCompanyFinancials skipped: empty symbol provided")
        return {}

    try:
        ticker = yf.Ticker(trimmed_symbol)
        info = ticker.info

        if not info:
            logger.info("GetCompanyFinancials returned no data for symbol: '%s'", trimmed_symbol)
            return {}

        result: dict[str, Any] = {
            "symbol": trimmed_symbol,
            "company_name": info.get("longName") or info.get("shortName") or trimmed_symbol,
        }

        # Valuation metrics
        valuation: dict[str, Any] = {}
        if (market_cap := info.get("marketCap")) is not None:
            valuation["market_cap"] = market_cap
        if (enterprise_value := info.get("enterpriseValue")) is not None:
            valuation["enterprise_value"] = enterprise_value
        if (trailing_pe := info.get("trailingPE")) is not None:
            valuation["pe_ratio_trailing"] = round(float(trailing_pe), 2)
        if (forward_pe := info.get("forwardPE")) is not None:
            valuation["pe_ratio_forward"] = round(float(forward_pe), 2)
        if (price_to_book := info.get("priceToBook")) is not None:
            valuation["price_to_book"] = round(float(price_to_book), 2)
        if valuation:
            result["valuation"] = valuation

        # Revenue and profitability
        financials: dict[str, Any] = {}
        if (total_revenue := info.get("totalRevenue")) is not None:
            financials["total_revenue"] = total_revenue
        if (revenue_growth := info.get("revenueGrowth")) is not None:
            financials["revenue_growth"] = f"{float(revenue_growth) * 100:.1f}%"
        if (gross_margins := info.get("grossMargins")) is not None:
            financials["gross_margin"] = f"{float(gross_margins) * 100:.1f}%"
        if (operating_margins := info.get("operatingMargins")) is not None:
            financials["operating_margin"] = f"{float(operating_margins) * 100:.1f}%"
        if (profit_margins := info.get("profitMargins")) is not None:
            financials["profit_margin"] = f"{float(profit_margins) * 100:.1f}%"
        if (return_on_equity := info.get("returnOnEquity")) is not None:
            financials["return_on_equity"] = f"{float(return_on_equity) * 100:.1f}%"
        if financials:
            result["financials"] = financials

        # Dividend info
        if (dividend_yield := info.get("dividendYield")) is not None:
            result["dividend_yield"] = f"{float(dividend_yield) * 100:.2f}%"

        # Analyst recommendations
        if info.get("recommendationKey"):
            result["analyst_recommendation"] = info.get("recommendationKey")
        if info.get("targetMeanPrice"):
            result["target_price"] = info.get("targetMeanPrice")
        if info.get("numberOfAnalystOpinions"):
            result["analyst_count"] = info.get("numberOfAnalystOpinions")

        return result

    except Exception as exc:
        logger.warning("GetCompanyFinancials failed for '%s': %s", trimmed_symbol, exc)
        return {}


def create_get_stock_info(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a stock info tool function bound to capabilities.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def get_stock_info_tool(symbol: str) -> dict[str, Any]:
        """Get stock price and company profile for a public company.

        Use this tool when you need current stock price, company overview,
        or basic company information for a publicly traded company. Great for
        understanding a prospect's market position, size, and industry before
        sales conversations.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL" for Apple, "MSFT" for
                    Microsoft, "CRM" for Salesforce, "GOOGL" for Google).

        Returns:
            Dictionary containing:
            - symbol: The stock ticker
            - company_name: Full company name
            - current_price: Latest stock price
            - market_cap: Market capitalization
            - sector: Business sector
            - industry: Specific industry
            - website: Company website
            - employees: Number of employees
            - description: Business summary
        """
        logger.info(f"[TOOL CALLED] get_stock_info - symbol='{symbol}'")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetStockInfo",
            tool_func=get_stock_info,
            parameters={"symbol": symbol},
            description=f"Getting stock info for: {symbol}",
            fallback_value={},
        )

        logger.info(f"[TOOL RESULT] get_stock_info returned {len(result) if result else 0} fields")
        return dict(result) if result else {}

    return get_stock_info_tool


def create_get_company_financials(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a company financials tool function bound to capabilities.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def get_company_financials_tool(symbol: str) -> dict[str, Any]:
        """Get financial metrics and analyst recommendations for a public company.

        Use this tool when you need detailed financial analysis of a prospect
        company - revenue growth, profit margins, valuation ratios, and analyst
        opinions. Helpful for understanding a company's financial health and
        preparing for business conversations.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL" for Apple, "MSFT" for
                    Microsoft, "CRM" for Salesforce, "GOOGL" for Google).

        Returns:
            Dictionary containing:
            - valuation: Market cap, P/E ratios, price-to-book
            - financials: Revenue, margins, profitability metrics
            - dividend_yield: If applicable
            - analyst_recommendation: Buy/Hold/Sell consensus
            - target_price: Analyst price target
        """
        logger.info(f"[TOOL CALLED] get_company_financials - symbol='{symbol}'")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetCompanyFinancials",
            tool_func=get_company_financials,
            parameters={"symbol": symbol},
            description=f"Getting financial data for: {symbol}",
            fallback_value={},
        )

        logger.info(
            f"[TOOL RESULT] get_company_financials returned {len(result) if result else 0} fields"
        )
        return dict(result) if result else {}

    return get_company_financials_tool


__all__ = [
    "get_stock_info",
    "get_company_financials",
    "create_get_stock_info",
    "create_get_company_financials",
]
