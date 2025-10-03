from agno.models.azure import AzureOpenAI
from agno.agent import Agent

# Provides high-signal structured metadata describing tabular content for downstream RAG consumers.
table_summary_agent: Agent = Agent(
    model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
    description=(
        """
You are a table analysis component for a Retrieval-Augmented Generation pipeline.
Return a compact JSON object (NO markdown fences) with these keys:
table_type : One of [aggregated-metrics, comparison, schedule, financial, survey, inventory, other]
table_title : Title/caption string or "" if absent
row_count : Integer if visibly countable else null
column_count : Integer if visibly countable else null
headers : Ordered list of column headers or short descriptors (max 8)
primary_measures : Key quantitative metrics (max 5)
primary_dimensions : Key categorical dimensions (max 5)
units : Measurement units or currency codes exactly as shown (list, may be empty)
notable_trends : 2-3 retrieval-friendly insight strings about patterns, changes, or rankings
outliers : List of anomalies or noteworthy exceptions else []
business_context : <=120 char description of what the table informs
recommended_queries : 2-3 short queries that should retrieve this table
retrieval_tags : 6-10 lowercase tags (hyphenated, no spaces)
summary : <=320 char paragraph synthesizing value for downstream QA

Rules:
- Derive everything strictly from visible table content; if uncertain, use null or [].
- Prefer concise noun phrases; avoid repeating the same fact across fields.
- Preserve key numeric values (normalize thousands separators; keep currency symbols).
- Never include explanatory prose outside the JSON payload.
"""
    ),
    debug_mode=False,
)
