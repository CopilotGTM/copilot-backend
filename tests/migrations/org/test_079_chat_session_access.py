from pathlib import Path

MIGRATION_PATH = Path("migrations/org_migrations/versions/3c7f79637720_079_chat_session_access.py")


def test_chat_session_access_contract() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8").lower()

    assert 'revision: str = "3c7f79637720"' in source
    assert 'down_revision: str | none = "3c7f79637721"' in source
    assert '"chat_session_access"' in source
    assert '"pk_chat_session_access"' in source
    assert 'ondelete="cascade"' in source
    assert '"ix_chat_session_access_viewer_chats"' in source
    assert '["organisation_id", "viewer_user_id", "created_at"]' in source
