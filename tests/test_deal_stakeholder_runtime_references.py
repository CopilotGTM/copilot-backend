from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_RUNTIME_REFERENCES = {
    Path("src/domain/models/organisation_tables/sync/crm/stakeholders.py"),
}

OLD_TABLE_PATTERNS = (
    re.compile(
        r"from src\.domain\.models\.organisation_tables\.sync\.crm\.stakeholders "
        r"import \([^)]*\bDealStakeholder\b",
        re.MULTILINE | re.DOTALL,
    ),
    re.compile(
        r"from src\.domain\.models\.organisation_tables\.sync\.crm\.stakeholders "
        r"import[^\n]*\bDealStakeholder\b",
    ),
    re.compile(r"\bselect\(\s*DealStakeholder\b"),
    re.compile(r"\bDealStakeholder\("),
    re.compile(r"\bupdate\(\s*DealStakeholder\b"),
    re.compile(r"\bdelete\(\s*DealStakeholder\b"),
)


def test_runtime_code_does_not_import_or_query_old_deal_stakeholder_model() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        relative_path = path.relative_to(ROOT)
        if relative_path in ALLOWED_RUNTIME_REFERENCES:
            continue

        text = path.read_text(encoding="utf-8")
        for pattern in OLD_TABLE_PATTERNS:
            if pattern.search(text):
                offenders.append(str(relative_path).replace("\\", "/"))
                break

    assert offenders == []
