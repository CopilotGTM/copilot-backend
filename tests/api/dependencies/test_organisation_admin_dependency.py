from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from src.api.dependencies.organisation_dependencies import require_organisation_admin
from src.domain.models.master_tables import UserAppRole


def test_require_organisation_admin_rejects_member() -> None:
    with pytest.raises(HTTPException) as error:
        require_organisation_admin(SimpleNamespace(user_role=UserAppRole.MEMBER))

    assert error.value.status_code == 403


def test_require_organisation_admin_returns_admin() -> None:
    admin = SimpleNamespace(user_role=UserAppRole.ADMIN)

    assert require_organisation_admin(admin) is admin
