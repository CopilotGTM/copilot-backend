from fastapi import APIRouter, Depends
from sqlmodel import Session

from src.domain.models.user_models import UserLoginRequest, UserLoginResponse
from src.infra.utils.app_state import fastapp
from src.infra.auth.user_auth import user_auth
from src.api.schemas.base_models import GenericResponse

router = APIRouter()

@router.post(
    "/login",
    response_model=GenericResponse[UserLoginResponse],
    tags=["user_auth"]
)
async def user_login(
    login_request: UserLoginRequest,
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    User login endpoint.
    """
    return user_auth.login(login_request, master_session)