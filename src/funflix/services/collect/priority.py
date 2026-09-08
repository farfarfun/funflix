"""采集源优先级。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, or_

from funflix.base.enums import SourceType
from funflix.models import Source

LOSS_SENSITIVE_MAX_GAP = timedelta(hours=12)
LOSS_SENSITIVE_SOURCE_TYPES = (SourceType.RSS, SourceType.WEB)


def loss_sensitive_source_clause(now: datetime) -> ColumnElement[bool]:
    """RSS/网页窗口不可回溯，超过 12 小时未成功采集时优先。"""
    return Source.source_type.in_(LOSS_SENSITIVE_SOURCE_TYPES) & or_(
        Source.last_success_at.is_(None),
        Source.last_success_at <= now - LOSS_SENSITIVE_MAX_GAP,
    )
