"""短语过滤规则的测试。

对照 albertorsesc/pain-discovery 的行为验证,同时锁定我们自己做的两处修正:
  1. 词边界 —— 防止 "am i being" 误配 "am i beings"
  2. 工具信号 —— 参考实现注释写了 "need dollar/tool signal",但只实现了 dollar
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.phrases import load_phrase_set, passes_keyword_filter


@pytest.fixture(scope="module")
def en():
    return load_phrase_set("en")


@pytest.fixture(scope="module")
def zh():
    return load_phrase_set("zh")


class TestEnglish:
    def test_tier1_keeps_without_evidence(self, en):
        assert passes_keyword_filter("Is this price fair?", en).keep
        assert passes_keyword_filter("my tool keeps crashing constantly", en).keep

    def test_tier2_needs_evidence(self, en):
        verdict = passes_keyword_filter("is this normal?", en)
        assert not verdict.keep
        assert verdict.tier == 2

    def test_tier2_with_dollar_keeps(self, en):
        assert passes_keyword_filter("is this normal? it cost me $500", en).keep

    def test_tier2_with_price_signal_keeps(self, en):
        assert passes_keyword_filter("worth it? my budget is tight", en).keep

    def test_tier2_with_tool_signal_keeps(self, en):
        # 我们的修正:参考实现没有 tool 信号
        assert passes_keyword_filter("what do you use for this? looking for a crm", en).keep

    def test_noise_dropped(self, en):
        assert not passes_keyword_filter("hello world", en).keep

    def test_word_boundary_prevents_false_match(self, en):
        # 误配出 tier2 但没有佐证 → 仍然丢弃
        assert not passes_keyword_filter("am i beings worried", en).keep

    def test_variant_suffix_matches(self, en):
        # 我们的修正:参考实现漏掉 "crashing" 这类词形变体
        assert passes_keyword_filter("it keeps crashing every time", en).keep


class TestChinese:
    def test_tier1_keeps(self, zh):
        assert passes_keyword_filter("这个软件太难用了,求推荐", zh).keep
        assert passes_keyword_filter("太贵了,三千多", zh).keep

    def test_tier2_with_tool_signal_keeps(self, zh):
        assert passes_keyword_filter("有没有人推荐个记账软件", zh).keep
        assert passes_keyword_filter("想找个项目管理工具", zh).keep

    def test_tier2_without_evidence_dropped(self, zh):
        assert not passes_keyword_filter("有没有人遇到吗", zh).keep
        assert not passes_keyword_filter("求推荐", zh).keep

    def test_noise_dropped(self, zh):
        for text in ("今天天气不错", "分享一张照片", "宝宝今天特别可爱"):
            assert not passes_keyword_filter(text, zh).keep, text

    def test_single_char_price_words_do_not_overfire(self, zh):
        # 「块」「元」是单字,极易误伤。必须只靠 tier2 命中才起作用
        assert not passes_keyword_filter("这个包块真好看", zh).keep
        assert not passes_keyword_filter("我用python写了个小工具", zh).keep


class TestRuleSets:
    def test_no_duplicate_phrases(self, en, zh):
        for ps in (en, zh):
            for name in ("tier1", "tier2", "price", "tool"):
                phrases = getattr(ps, name)
                assert len(phrases) == len(set(phrases)), f"{ps.lang}/{name} 有重复"

    def test_no_overlap_between_tiers(self, en, zh):
        for ps in (en, zh):
            assert not (set(ps.tier1) & set(ps.tier2))

    def test_empty_text_dropped(self, en, zh):
        for ps in (en, zh):
            assert not passes_keyword_filter("", ps).keep
            assert not passes_keyword_filter("   ", ps).keep

    def test_counts_match_documented_values(self, en, zh):
        """词表条数与 ARCHITECTURE.md §5.2.5 里写的数字一致。

        文档里的条数是给读者看的规模参考,漂移了就说明文档没跟上代码。
        """
        expected = {
            "en": {"tier1": 264, "tier2": 62, "price": 57, "tool": 47},
            "zh": {"tier1": 252, "tier2": 113, "price": 85, "tool": 54},
        }
        for ps in (en, zh):
            for name, count in expected[ps.lang].items():
                actual = len(getattr(ps, name))
                assert actual == count, (
                    f"{ps.lang}/{name} 实际 {actual} 条,文档写 {count} 条"
                )
