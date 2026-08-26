"""SQLAlchemy 模型。

导入本模块即完成全部表在 `Base.metadata` 上的注册 ——
Alembic 的 env.py 依赖这一点来做 autogenerate。
"""

from funflix.models.association import media_resource
from funflix.models.base import Base, TimestampMixin, UTCDateTime, utcnow
from funflix.models.check import LinkCheck
from funflix.models.extraction import Extraction
from funflix.models.media import UNKNOWN_YEAR, Media
from funflix.models.raw import RawDocument
from funflix.models.resource import Resource
from funflix.models.source import Source
from funflix.models.tag import Tag, TagKind, media_tag

__all__ = [
    "UNKNOWN_YEAR",
    "Base",
    "Extraction",
    "LinkCheck",
    "Media",
    "RawDocument",
    "Resource",
    "Source",
    "Tag",
    "TagKind",
    "media_resource",
    "media_tag",
    "TimestampMixin",
    "UTCDateTime",
    "utcnow",
]
