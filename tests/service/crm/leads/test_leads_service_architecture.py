"""Architecture checks for the CRM leads service package boundary."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_lead_related_services_do_not_live_in_deprecated_access_package() -> None:
    access_dir = ROOT / "src/service/crm/access"
    lead_files = [
        path.name
        for path in access_dir.glob("*lead*.py")
        if path.name != "__init__.py" and path.name != "deals_access_service.py"
    ]

    assert lead_files == []


def test_lead_code_imports_domain_services_directly() -> None:
    offenders: list[str] = []
    for base in ("src", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            if ".venv" in path.parts:
                continue
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            if (
                "src.service.crm.access.lead" in text
                or "src.service.crm.access.private_lead" in text
            ):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_lead_routers_do_not_own_presenter_helpers() -> None:
    router_helpers = {
        "src/api/routers/crm/leads/leads_handler.py": [
            "def _lead_item",
            "def _participant_item",
            "def _lead_detail",
            "def _conversion_response",
        ],
        "src/api/routers/crm/leads/lead_forms_handler.py": [
            "def _field_item",
            "def _public_form_url",
            "def _form_item",
            "def _public_error_code",
            "def _log_public_form_failure",
            "def _public_error_response",
            "def _public_rate_limit_error_response",
            "def _public_unexpected_error_response",
        ],
    }

    offenders = [
        f"{path}:{helper}"
        for path, helpers in router_helpers.items()
        for helper in helpers
        if helper in _read(path)
    ]

    assert offenders == []


def test_lead_read_service_stays_read_only() -> None:
    text = _read("src/service/crm/leads/read_service.py")
    forbidden_methods = [
        "async def create_lead",
        "async def update_lead",
        "async def add_participant",
        "async def convert_lead",
    ]

    offenders = [method for method in forbidden_methods if method in text]

    assert offenders == []
