from datetime import UTC, datetime

from src.service.crm.utils.external_links import (
    extract_linkedin_url,
    serialize_external_links,
)


class LinkDto:
    platform = "linkedin"
    url = " https://linkedin.com/in/buyer "
    label = "Buyer"
    added_at = datetime(2026, 1, 2, tzinfo=UTC)


def test_serialize_external_links_accepts_dicts_and_dtos() -> None:
    raw = {"platform": "twitter", "url": "https://x.com/buyer"}

    assert serialize_external_links([raw, LinkDto()]) == [
        raw,
        {
            "platform": "linkedin",
            "url": " https://linkedin.com/in/buyer ",
            "label": "Buyer",
            "added_at": "2026-01-02T00:00:00+00:00",
        },
    ]


def test_extract_linkedin_url_prefers_linkedin_platform_or_url() -> None:
    assert (
        extract_linkedin_url(
            [
                {"platform": "website", "url": "https://example.com"},
                {"platform": "LinkedIn", "url": " https://linkedin.com/in/buyer "},
            ]
        )
        == "https://linkedin.com/in/buyer"
    )
    assert extract_linkedin_url([{"type": "profile", "url": "https://linkedin.com/in/buyer"}])
