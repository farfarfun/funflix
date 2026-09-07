"""FastAPI 依赖。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session
from funflix.models import User
from funflix.schemas.common import MAX_PAGE_NUMBER, MAX_PAGE_SIZE

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(slots=True)
class PageParams:
    """翻页入参。

    四个列表接口曾各写一遍 page/size 的声明与 `(page - 1) * size`，
    上限已经先漂了一次（两处 100、两处 200）。收敛到这里，
    翻页语义只有一个定义点。
    """

    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def page_params(
    page: Annotated[int, Query(ge=1, le=MAX_PAGE_NUMBER, description="页码，从 1 开始")] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="每页条数")] = 20,
) -> PageParams:
    return PageParams(page=page, size=size)


PageDep = Annotated[PageParams, Depends(page_params)]


async def get_or_404(session: AsyncSession, model: type[Any], pk: Any, detail: str) -> Any:
    """按主键取，取不到就 404。"""
    row = await session.get(model, pk)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return row


async def get_current_user(request: Request, session: SessionDep) -> User:
    """「运维」区接口的鉴权：要求已登录会话。"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    user = await session.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录"
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
