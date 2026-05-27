from src.domain.models.organisation_tables.sync.calendar import CalendarEventUserState


def test_calendar_user_state_external_id_allows_long_microsoft_ids() -> None:
    column = CalendarEventUserState.__table__.c.external_id

    assert column.type.length == 1024
