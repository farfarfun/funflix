"""FastAPI 依赖。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_admin(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """管理类接口的鉴权。

    未配置 `FUNFLIX_ADMIN_API_KEY` 时直接拒绝 —— 默认关闭比默认放行安全。
    """
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理接口未启用：请配置 FUNFLIX_ADMIN_API_KEY",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 API Key")


AdminDep = Annotated[None, Depends(require_admin)]
