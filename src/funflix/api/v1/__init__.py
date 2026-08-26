"""v1 路由聚合。"""

from fastapi import APIRouter

from funflix.api.v1 import media, raw, resources, sources, stats

api_router = APIRouter()
api_router.include_router(sources.router)
api_router.include_router(raw.router)
api_router.include_router(media.router)
api_router.include_router(resources.router)
api_router.include_router(stats.router)

__all__ = ["api_router"]
