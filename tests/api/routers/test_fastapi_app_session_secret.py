import importlib

import pytest
from starlette.middleware.sessions import SessionMiddleware


def test_create_fastapi_app_uses_runtime_session_secret(mocker) -> None:
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    mocker.patch.object(
        fastapi_app.security_secrets_config,
        "get_session_secret_sync",
        return_value="runtime-session-secret",
    )
    mocker.patch.object(fastapi_app, "instrument_fastapi_app", return_value=None)

    app = fastapi_app.create_fastapi_app()
    session_middleware = next(
        middleware for middleware in app.user_middleware if middleware.cls is SessionMiddleware
    )

    assert session_middleware.kwargs["secret_key"] == "runtime-session-secret"


def test_create_fastapi_app_requires_runtime_session_secret(mocker) -> None:
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    mocker.patch.object(
        fastapi_app.security_secrets_config,
        "get_session_secret_sync",
        side_effect=RuntimeError("missing session secret"),
    )
    mocker.patch.object(fastapi_app, "instrument_fastapi_app", return_value=None)

    with pytest.raises(RuntimeError, match="missing session secret"):
        fastapi_app.create_fastapi_app()
