"""v1 路由聚合。"""

from fastapi import APIRouter

from funflix.api.v1 import raw, sources

api_router = APIRouter()
api_router.include_router(sources.router)
api_router.include_router(raw.router)

__all__ = ["api_router"]
