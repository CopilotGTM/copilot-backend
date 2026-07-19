"""Orchestrates existing crawl and Exa infrastructure for Kai Training."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.agents.tools.research.web_crawler import crawl_webpage
from src.agents.tools.search.internet_search import internet_search
from src.agents.workflows.kai_training import KaiTrainingResearchAgent
from src.domain.models.master_tables import Organisation
from src.infra.database.sessions import master_async_session_scope
from src.service.orgnisation.kai_training_service import KaiTrainingService


@dataclass(frozen=True)
class _OrganisationResearchSnapshot:
    name: str
    website: str | None
    domain: str | None


class KaiTrainingResearchService:
    def __init__(self, organisation_id: UUID, run_id: UUID):
        self.organisation_id = organisation_id
        self.run_id = run_id

    async def run(self) -> bool:
        snapshot = await self._load_snapshot()
        async with master_async_session_scope() as session:
            if not await KaiTrainingService(session, self.organisation_id).mark_in_progress(
                self.run_id
            ):
                return False

        crawl_result, search_results = await asyncio.gather(
            crawl_webpage(snapshot.website) if snapshot.website else _empty_crawl(),
            self._search(snapshot),
        )
        evidence, sources = self._evidence(crawl_result, search_results)
        if not evidence:
            raise ValueError("No public research evidence was found")

        output = await KaiTrainingResearchAgent().synthesize(
            organisation_name=snapshot.name,
            evidence=evidence,
        )
        async with master_async_session_scope() as session:
            return await KaiTrainingService(session, self.organisation_id).persist_research(
                self.run_id, output.model_dump(), sources
            )

    async def _load_snapshot(self) -> _OrganisationResearchSnapshot:
        async with master_async_session_scope() as session:
            organisation = await session.get(Organisation, self.organisation_id)
            if not organisation:
                raise ValueError("Organisation not found")
            website = next(
                (str(url).strip() for url in organisation.website_urls or [] if url), None
            )
            domain = next(
                (str(value).strip() for value in organisation.domain or [] if value), None
            )
            return _OrganisationResearchSnapshot(
                name=organisation.organisation_name,
                website=website or (f"https://{domain}" if domain else None),
                domain=domain,
            )

    async def _search(self, snapshot: _OrganisationResearchSnapshot) -> list[dict[str, str]]:
        identity = snapshot.domain or snapshot.name
        queries = (
            f'"{identity}" company overview',
            f'"{identity}" products services',
            f'"{identity}" customers market',
            f'"{identity}" why choose case studies',
        )
        result_sets = await asyncio.gather(
            *(internet_search(query, max_results=3, category="company") for query in queries)
        )
        return [item for results in result_sets for item in results]

    @staticmethod
    def _evidence(
        crawl_result: dict[str, Any],
        search_results: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        evidence: list[dict[str, str]] = []
        sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()

        crawl_url = str(crawl_result.get("url") or "")
        crawl_content = str(crawl_result.get("content") or "")
        if crawl_url and crawl_content:
            evidence.append({"url": crawl_url, "content": crawl_content[:5000]})
            sources.append(
                {"label": str(crawl_result.get("title") or crawl_url)[:500], "url": crawl_url}
            )
            seen_urls.add(crawl_url)

        for result in search_results:
            url = str(result.get("url") or "")
            snippet = str(result.get("snippet") or "")
            if not url or not snippet or url in seen_urls:
                continue
            evidence.append({"url": url, "content": snippet[:4000]})
            sources.append({"label": str(result.get("label") or url)[:500], "url": url})
            seen_urls.add(url)
            if len(evidence) >= 13:
                break
        return evidence, sources


async def _empty_crawl() -> dict[str, Any]:
    return {}
