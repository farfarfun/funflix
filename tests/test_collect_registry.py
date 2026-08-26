"""采集器注册表：URL 分发与持久化兼容。

分发顺序曾经是 `detect_source` 里手写的一个类型元组
`(TENCENT_DOCS, TENCENT_DOC, TELEGRAM)` —— 两个名字只差一个 s，
调换或写错一个字母就会把每一条 URL 都路由到错误的采集器上，
而且不会报错：智能表格会被文本采集器当成一篇文档去解析，抽出一堆空结果。

现在顺序由各采集器自己声明的 `detect_priority` 决定，这些测试盯住它。
"""

from __future__ import annotations

import pytest

from funflix.base.enums import SourceType
from funflix.services.collect.registry import (
    detect_source,
    get_collector_class,
    supported_source_types,
)

SHEET_ID = "DT0xZd3Vsc0hFWGpN"


class TestDispatch:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (f"https://docs.qq.com/smartsheet/{SHEET_ID}", SourceType.TENCENT_DOCS),
            (f"https://docs.qq.com/sheet/{SHEET_ID}", SourceType.TENCENT_DOCS),
            (f"https://docs.qq.com/doc/{SHEET_ID}", SourceType.TENCENT_DOC),
            ("https://t.me/s/SomeChannel", SourceType.TELEGRAM),
        ],
    )
    def test_url_routes_to_the_right_collector(self, url: str, expected: SourceType) -> None:
        result = detect_source(url)
        assert result is not None, f"{url} 没被任何采集器认领"
        assert result[0] is expected

    def test_telegram_does_not_steal_tencent_urls(self) -> None:
        """Telegram 的模式能匹配任意裸标识串，必须最后问。"""
        result = detect_source(f"https://docs.qq.com/smartsheet/{SHEET_ID}")
        assert result is not None
        assert result[0] is not SourceType.TELEGRAM

    def test_unknown_url_returns_none(self) -> None:
        assert detect_source("https://example.com/whatever") is None


class TestPriorityOrdering:
    def test_every_collector_declares_a_priority(self) -> None:
        """漏声明的采集器会拿到默认值 100，可能悄悄插到 Telegram 前面。"""
        for source_type in supported_source_types():
            cls = get_collector_class(source_type)
            assert hasattr(cls, "detect_priority"), f"{source_type.value} 没声明 detect_priority"

    def test_priorities_are_unique(self) -> None:
        """两个采集器同优先级时，谁先被问取决于字典顺序 —— 那就又变成隐式依赖了。"""
        priorities = [
            get_collector_class(s).detect_priority  # type: ignore[union-attr]
            for s in supported_source_types()
        ]
        assert len(set(priorities)) == len(priorities), f"优先级有重复：{priorities}"

    def test_telegram_is_last(self) -> None:
        telegram = get_collector_class(SourceType.TELEGRAM).detect_priority  # type: ignore[union-attr]
        others = [
            get_collector_class(s).detect_priority  # type: ignore[union-attr]
            for s in supported_source_types()
            if s is not SourceType.TELEGRAM
        ]
        assert all(telegram > p for p in others), "Telegram 的兜底模式必须最后问"

    def test_sheet_is_asked_before_text_doc(self) -> None:
        """两者同域名，智能表格的模式更具体，先问。"""
        sheet = get_collector_class(SourceType.TENCENT_DOCS).detect_priority  # type: ignore[union-attr]
        text = get_collector_class(SourceType.TENCENT_DOC).detect_priority  # type: ignore[union-attr]
        assert sheet < text


class TestPersistedValuesAreStable:
    """`source_type` 的字面值落在 source 与 raw_document 两张表里。

    模块和类可以随便改名，**枚举值不能动** —— 一改，库里已有的行就再也
    匹配不上任何采集器，那些源会静默地停止采集。

    值里的 `docs`/`doc` 是历史命名（智能表格 = tencent_docs），
    代码里已改叫 sheet/text；想让两边一致得配一次数据迁移。
    """

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (SourceType.TENCENT_DOCS, "tencent_docs"),
            (SourceType.TENCENT_DOC, "tencent_doc"),
            (SourceType.TELEGRAM, "telegram"),
        ],
    )
    def test_value_is_unchanged(self, member: SourceType, value: str) -> None:
        assert member.value == value

    def test_sheet_maps_to_sheet_collector(self) -> None:
        from funflix.services.collect.tencent_sheet import TencentSheetCollector

        assert get_collector_class(SourceType.TENCENT_DOCS) is TencentSheetCollector

    def test_text_doc_maps_to_text_collector(self) -> None:
        from funflix.services.collect.tencent_text import TencentTextCollector

        assert get_collector_class(SourceType.TENCENT_DOC) is TencentTextCollector
