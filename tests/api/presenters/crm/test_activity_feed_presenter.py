"""Activity-feed response presentation tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.api.presenters.crm.activity_feed_presenter import present_activity_feed_item
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityLink
from src.service.crm.activity_feed_read_service import ActivityFeedRecord


def test_activity_feed_item_groups_related_object_roles_for_frontend_navigation() -> None:
    organisation_id = uuid4()
    activity_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    occurred_at = datetime(2026, 7, 1, tzinfo=UTC)
    activity = CRMActivity(
        activity_id=activity_id,
        organisation_id=organisation_id,
        activity_type="EMAIL",
        source_type="EMAIL",
        source_record_id=str(uuid4()),
    )
    contact_link = CRMActivityLink(
        organisation_id=organisation_id,
        activity_id=activity_id,
        object_type="CONTACT",
        object_id=contact_id,
        link_role="SENDER",
        link_source="SYSTEM",
        occurred_at=occurred_at,
    )
    second_contact_role = contact_link.model_copy(deep=True)
    second_contact_role.activity_link_id = uuid4()
    second_contact_role.link_role = "RELATED"
    deal_link = contact_link.model_copy(deep=True)
    deal_link.activity_link_id = uuid4()
    deal_link.object_type = "DEAL"
    deal_link.object_id = deal_id
    deal_link.link_role = "PRIMARY"

    item = present_activity_feed_item(
        ActivityFeedRecord(
            activity=activity,
            link=contact_link,
            occurred_at=occurred_at + timedelta(minutes=5),
            related_links=(contact_link, second_contact_role, deal_link),
        )
    )

    assert item.occurred_at == occurred_at + timedelta(minutes=5)
    assert [
        (target.object_type, target.object_id, target.link_roles) for target in item.related_objects
    ] == [
        ("CONTACT", contact_id, ["RELATED", "SENDER"]),
        ("DEAL", deal_id, ["PRIMARY"]),
    ]
