from pydantic import BaseModel, EmailStr


class CreateChat(BaseModel):
    title: str


class AccessTokenResponse(BaseModel):
    access_token: str = None
    refresh_token: str = None
    token_type: str
    expires_in: int
