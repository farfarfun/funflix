from __future__ import annotations

import pytest

from funflix.base.enums import Provider
from funflix.services.text.linkscan import identify_provider, scan_known_links, scan_links


class TestIdentifyProvider:
    @pytest.mark.parametrize(
        ("url", "provider", "share_id"),
        [
            ("https://pan.quark.cn/s/abc123def456", Provider.QUARK, "abc123def456"),
            ("https://drive.uc.cn/s/xyz789", Provider.UC, "xyz789"),
            ("https://www.alipan.com/s/AbCd1234", Provider.ALIPAN, "AbCd1234"),
            ("https://www.aliyundrive.com/s/AbCd1234", Provider.ALIPAN, "AbCd1234"),
            ("https://www.alipan.com/t/TtTt1111", Provider.ALIPAN, "TtTt1111"),
            ("https://pan.baidu.com/s/1AbCdEf-gh", Provider.BAIDU, "AbCdEf-gh"),
            ("https://pan.baidu.com/share/init?surl=AbCdEf", Provider.BAIDU, "AbCdEf"),
            ("https://115.com/s/sw1234abc", Provider.PAN115, "sw1234abc"),
            ("https://cloud.189.cn/t/QqQq22", Provider.TIANYI, "QqQq22"),
            ("https://pan.xunlei.com/s/VN_abc-123", Provider.XUNLEI, "VN_abc-123"),
            ("https://www.lanzoux.com/iAbCd12", Provider.LANZOU, "iAbCd12"),
            (
                "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                Provider.MAGNET,
                "0123456789abcdef0123456789abcdef01234567",
            ),
        ],
    )
    def test_recognizes_each_provider(self, url: str, provider: Provider, share_id: str) -> None:
        assert identify_provider(url) == (provider, share_id)

    def test_baidu_strips_leading_one_from_share_id(self) -> None:
        """百度分享路径固定以 1 开头，那个 1 不属于 share_id。"""
        assert identify_provider("https://pan.baidu.com/s/1abcdef") == (Provider.BAIDU, "abcdef")

    def test_unknown_host_returns_none(self) -> None:
        assert identify_provider("https://example.com/s/abc") is None


class TestScanLinks:
    def test_finds_multiple_links_in_one_text(self) -> None:
        """一条文本里的链接必须全部返回，绝不截断。"""
        text = (
            "剧集A\n"
            "夸克：https://pan.quark.cn/s/aaa111\n"
            "阿里：https://www.alipan.com/s/bbb222\n"
            "百度：https://pan.baidu.com/s/1ccc333\n"
        )
        links = scan_links(text)
        assert [link.provider for link in links] == [
            Provider.QUARK,
            Provider.ALIPAN,
            Provider.BAIDU,
        ]
        assert [link.share_id for link in links] == ["aaa111", "bbb222", "ccc333"]

    def test_preserves_order_of_appearance(self) -> None:
        text = "https://www.alipan.com/s/second 前面还有 https://pan.quark.cn/s/first"
        links = scan_links(text)
        assert [link.share_id for link in links] == ["second", "first"]

    def test_records_span_in_original_text(self) -> None:
        text = "链接：https://pan.quark.cn/s/abc123"
        link = scan_links(text)[0]
        assert text[link.start : link.end] == "https://pan.quark.cn/s/abc123"

    def test_deduplicates_same_share_within_one_text(self) -> None:
        """文案常把同一个链接贴两遍，不该产出两条资源。"""
        text = "https://pan.quark.cn/s/same1 ... 再贴一次 https://pan.quark.cn/s/same1"
        assert len(scan_links(text)) == 1

    def test_different_shares_are_kept_separately(self) -> None:
        text = "https://pan.quark.cn/s/one1 https://pan.quark.cn/s/two2"
        assert len(scan_links(text)) == 2

    def test_canonicalizes_magnet_without_trackers(self) -> None:
        base = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        raw = f"{base}&dn=Title&tr=https%3A%2F%2Ftracker.example%2Fannounce"
        link = scan_links(raw)[0]
        assert link.url == base
        assert link.raw_url == raw
        assert raw[link.start : link.end] == raw

    def test_returns_empty_for_text_without_links(self) -> None:
        assert scan_links("就是一段没有链接的文字") == []


class TestTrailingPunctuation:
    @pytest.mark.parametrize(
        "text",
        [
            "链接：https://pan.quark.cn/s/abc123。",
            "链接：https://pan.quark.cn/s/abc123，转存",
            "链接：https://pan.quark.cn/s/abc123、",
            "链接 https://pan.quark.cn/s/abc123.",
            "见 https://pan.quark.cn/s/abc123!",
        ],
    )
    def test_strips_trailing_punctuation(self, text: str) -> None:
        """中文文案里句号紧跟 URL 极常见，不剥会污染 share_id。"""
        link = scan_links(text)[0]
        assert link.share_id == "abc123"
        assert link.url == "https://pan.quark.cn/s/abc123"

    def test_strips_unpaired_closing_paren(self) -> None:
        link = scan_links("（链接 https://pan.quark.cn/s/abc123）")[0]
        assert link.share_id == "abc123"

    def test_keeps_paired_parens_inside_url(self) -> None:
        link = scan_links("https://example.com/a_(b)_c")[0]
        assert link.url == "https://example.com/a_(b)_c"


class TestPasscode:
    def test_extracts_passcode_from_url_query(self) -> None:
        link = scan_links("https://pan.baidu.com/s/1abcdef?pwd=8k2m")[0]
        assert link.passcode == "8k2m"

    @pytest.mark.parametrize(
        "text",
        [
            "https://pan.baidu.com/s/1abcdef 提取码: 8k2m",
            "https://pan.baidu.com/s/1abcdef 提取码：8k2m",
            "https://pan.baidu.com/s/1abcdef 密码 8k2m",
            "https://pan.baidu.com/s/1abcdef\n访问码：8k2m",
        ],
    )
    def test_extracts_passcode_near_link(self, text: str) -> None:
        assert scan_links(text)[0].passcode == "8k2m"

    def test_no_passcode_yields_none(self) -> None:
        assert scan_links("https://pan.quark.cn/s/abc123")[0].passcode is None

    def test_does_not_steal_passcode_from_a_distant_link(self) -> None:
        """提取码只在链接后方的小窗口里找，否则会串到下一条资源上。"""
        text = "https://pan.quark.cn/s/first1\n" + "填充。" * 40 + "\n提取码：9z8y"
        assert scan_links(text)[0].passcode is None


class TestUnknownProviders:
    def test_unknown_link_kept_as_other_by_default(self) -> None:
        links = scan_links("https://example.com/whatever")
        assert links[0].provider is Provider.OTHER

    def test_scan_known_links_drops_unknown(self) -> None:
        text = "https://example.com/x https://pan.quark.cn/s/abc123"
        links = scan_known_links(text)
        assert len(links) == 1
        assert links[0].provider is Provider.QUARK

    def test_hashes_long_unknown_share_id(self) -> None:
        url = "https://example.com/" + "x" * 300
        link = scan_links(url)[0]
        assert link.url == url
        assert link.share_id.startswith("sha256:")
        assert len(link.share_id) == 71

    def test_drops_unknown_url_too_long_for_storage(self) -> None:
        assert scan_links("https://example.com/" + "x" * 2048) == []
