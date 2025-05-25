from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, AsyncIterator, Iterable, Optional


@dataclass(slots=True)
class KBHit:  # individual document hit
    call_id: str
    query: str
    doc_name: str
    doc_page: Optional[int] = None
    doc_link: Optional[str] = None

    # convenience helpers
    @classmethod
    def from_tool_payload(cls, payload: Dict[str, Any]) -> Iterable["KBHit"]:
        """
        Yield KBHit objects from the raw `content` returned by `async_search_knowledge_base`.
        """
        call_id = payload.get("tool_call_id")
        tool_args = payload.get("tool_args", {})
        query = tool_args.get("query")
        content = payload.get("content")

        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return []

        for item in content or []:
            yield cls(
                call_id=call_id,
                query=query,
                doc_name=item.get("name"),
                doc_page=item.get("meta_data", {}).get("page"),
                doc_link=item.get("meta_data", {}).get("doc_link"),
            )


@dataclass(slots=True)
class KBHits:  # collection with set-like semantics
    hits: List[KBHit] = field(default_factory=list)

    def add_from_chunk(self, chunk: Any) -> None:
        """
        Inspect a streamed chunk; if it contains knowledge-base tool calls,
        merge the unique hits into `self.hits`.
        """
        if not (hasattr(chunk, "tools") and isinstance(chunk.tools, list)):
            return

        for td in chunk.tools:
            if isinstance(td, dict) and td.get("tool_name") == "async_search_knowledge_base":
                for hit in KBHit.from_tool_payload(td):
                    self._insert_unique(hit)

    async def stream(self, chunks: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """
        Wrap an existing async iterator of RunResponse chunks:
        • yields chunks unchanged
        • accumulates KB hits on the fly
        """
        async for chunk in chunks:
            self.add_from_chunk(chunk)
            yield chunk

    def _insert_unique(self, hit: KBHit) -> None:
        key = (hit.call_id, hit.doc_name, hit.doc_page)
        if key not in {(h.call_id, h.doc_name, h.doc_page) for h in self.hits}:
            self.hits.append(hit)

    def as_json(self) -> str:
        return json.dumps([asdict(h) for h in self.hits], ensure_ascii=False)

    # nice repr so it’s readable if you stick it straight into RunResponse.content
    def __str__(self) -> str:
        return self.as_json()
