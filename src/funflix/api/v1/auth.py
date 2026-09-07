"""登录 / 登出 / 当前用户 / 注册。

「运维」区其余路由都靠 `CurrentUserDep`（见 `api/deps.py`）挡在会话之后，
这个模块只负责建立/销毁那个会话。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from funflix.api.deps import SessionDep, SettingsDep
from funflix.models import User
from funflix.security import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginPayload(BaseModel):
    username: str
    password: str


class RegisterPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str

    model_config = {"from_attributes": True}


class AuthConfigOut(BaseModel):
    registration_enabled: bool


@router.get("/config", response_model=AuthConfigOut)
async def get_auth_config(settings: SettingsDep) -> AuthConfigOut:
    return AuthConfigOut(registration_enabled=settings.registration_enabled)


@router.post("/login", response_model=UserOut)
async def login(payload: LoginPayload, request: Request, session: SessionDep) -> UserOut:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    request.session["user_id"] = str(user.id)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(request: Request) -> None:
    request.session.clear()


@router.get("/me", response_model=UserOut | None)
async def me(request: Request, session: SessionDep) -> UserOut | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = await session.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return UserOut.model_validate(user)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterPayload, settings: SettingsDep, session: SessionDep) -> UserOut:
    if not settings.registration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="注册入口未开放")
    existing = await session.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    return UserOut.model_validate(user)
