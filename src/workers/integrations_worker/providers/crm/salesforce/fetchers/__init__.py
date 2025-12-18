"""Salesforce CRM fetchers."""

from __future__ import annotations

from .account_fetcher import SalesforceAccountFetcher
from .case_fetcher import SalesforceCaseFetcher
from .contact_fetcher import SalesforceContactFetcher
from .contact_role_fetcher import SalesforceContactRoleFetcher
from .lead_fetcher import SalesforceLeadFetcher
from .opportunity_fetcher import SalesforceOpportunityFetcher
from .pipeline_fetcher import SalesforcePipelineFetcher
from .task_fetcher import SalesforceTaskFetcher
from .user_fetcher import SalesforceUserFetcher

__all__ = [
    "SalesforceAccountFetcher",
    "SalesforceCaseFetcher",
    "SalesforceContactFetcher",
    "SalesforceContactRoleFetcher",
    "SalesforceLeadFetcher",
    "SalesforceOpportunityFetcher",
    "SalesforcePipelineFetcher",
    "SalesforceTaskFetcher",
    "SalesforceUserFetcher",
]
