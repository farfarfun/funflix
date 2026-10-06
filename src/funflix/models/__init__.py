"""SQLAlchemy 模型。

导入本模块即完成全部表在 `Base.metadata` 上的注册 ——
Alembic 的 env.py 依赖这一点来做 autogenerate。
"""

from funflix.models.association import media_resource
from funflix.models.base import Base, TimestampMixin, UTCDateTime, utcnow
from funflix.models.canon import CANON_PROMPT_VERSION, CanonState, TitleCanon
from funflix.models.check import LinkCheck
from funflix.models.extraction import Extraction
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR, Media
from funflix.models.raw import RawDocument
from funflix.models.repair import (
    RepairKind,
    RepairState,
    RepairSymptom,
    RepairTask,
)
from funflix.models.resource import Resource
from funflix.models.source import Source
from funflix.models.tag import Tag, TagKind, media_tag
from funflix.models.user import User
from funflix.models.work import Work

__all__ = [
    "CANON_PROMPT_VERSION",
    "NO_SEASON",
    "UNKNOWN_YEAR",
    "Base",
    "CanonState",
    "Extraction",
    "LinkCheck",
    "Media",
    "RawDocument",
    "RepairKind",
    "RepairState",
    "RepairSymptom",
    "RepairTask",
    "Resource",
    "Source",
    "Tag",
    "TagKind",
    "TitleCanon",
    "User",
    "Work",
    "media_resource",
    "media_tag",
    "TimestampMixin",
    "UTCDateTime",
    "utcnow",
]
