"""短语过滤规则 —— 采集层的第一道关卡。

移植自 albertorsesc/pain-discovery 的三层短语过滤,并做了一处修正:
参考实现用朴素子串匹配,会漏掉词形变体(如 "crashing" 匹配不到 "crashes")。
这里用「词边界正则 + 词干前缀匹配」兼顾召回与误报。

过滤链:
    Tier1 命中                          → 保留
    Tier2 命中 且 (价格信号 或 $金额)    → 保留
    否则                                 → 丢弃(不落库)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PHRASES_DIR = Path(__file__).resolve().parent.parent / "config" / "phrases"

# 变体后缀:命中词干 + 这些后缀也算命中(覆盖 is/ing/ed/s 等)
_VARIANT_SUFFIXES = ("s", "es", "ed", "ing", "d", "er", "ers", "ly", "n't")


@dataclass(frozen=True)
class PhraseSet:
    """一个平台的完整短语规则集。"""

    lang: str
    tier1: tuple[str, ...]
    tier2: tuple[str, ...]
    price: tuple[str, ...]
    tool: tuple[str, ...]
    _tier1_re: tuple[re.Pattern, ...]
    _tier2_re: tuple[re.Pattern, ...]
    _price_re: tuple[re.Pattern, ...]
    _tool_re: tuple[re.Pattern, ...]

    def match_tier1(self, text: str) -> list[str]:
        return [p for p, rx in zip(self.tier1, self._tier1_re) if rx.search(text)]

    def match_tier2(self, text: str) -> list[str]:
        return [p for p, rx in zip(self.tier2, self._tier2_re) if rx.search(text)]

    def match_price(self, text: str) -> list[str]:
        return [p for p, rx in zip(self.price, self._price_re) if rx.search(text)]

    def match_tool(self, text: str) -> list[str]:
        return [p for p, rx in zip(self.tool, self._tool_re) if rx.search(text)]


def _compile(phrases: tuple[str, ...], variant_suffix: bool) -> tuple[re.Pattern, ...]:
    """把短语编译成正则。

    variant_suffix=True 时额外匹配常见的词形变体(英文用;中文无词形变化,关掉)。
    """
    compiled = []
    for p in phrases:
        # 转义后按词边界锚定,避免 "am i being" 匹配到 "am i beings"
        pattern = re.escape(p).replace(r"\ ", r"\s+")
        if variant_suffix:
            pattern = rf"\b{pattern}(?:{'|'.join(re.escape(s) for s in _VARIANT_SUFFIXES)})?\b"
        else:
            pattern = rf"{pattern}"
        compiled.append(re.compile(pattern, re.IGNORECASE))
    return tuple(compiled)


def _load_phrases(path: Path) -> tuple[str, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(data.get("phrases") or ())


@lru_cache(maxsize=8)
def load_phrase_set(lang: str) -> PhraseSet:
    """加载某语言的短语规则集。中文用 zh/ 目录,英文用 en/。"""
    base = PHRASES_DIR / lang
    if not base.is_dir():
        raise FileNotFoundError(f"没有 {lang} 的短语规则目录: {base}")

    variant = lang == "en"
    tier1 = _load_phrases(base / "tier1.yaml")
    tier2 = _load_phrases(base / "tier2.yaml")
    price = _load_phrases(base / "price_signals.yaml")
    tool = _load_phrases(base / "tool_signals.yaml")

    return PhraseSet(
        lang=lang,
        tier1=tier1,
        tier2=tier2,
        price=price,
        tool=tool,
        _tier1_re=_compile(tier1, variant),
        _tier2_re=_compile(tier2, variant),
        _price_re=_compile(price, variant),
        _tool_re=_compile(tool, variant),
    )


_DOLLAR_RE = re.compile(r"\$\s?\d")
# 中文金额:「3000」「三千」「3000块」「¥3000」等
_CNY_RE = re.compile(r"[¥￥]\s?\d|[零一二两三四五六七八九十百千万]+\s*[元块]|(\d[\d,]*)\s*[元块]")


@dataclass(frozen=True)
class FilterVerdict:
    """过滤判定结果。"""

    keep: bool
    tier: int | None          # 1 或 2,None 表示未命中
    matched: list[str]        # 命中的短语
    evidence: list[str]       # Tier2 的佐证(Tier1 时为空)


def passes_keyword_filter(text: str, phrases: PhraseSet) -> FilterVerdict:
    """判定一条内容是否值得入库。

    TIER1 命中 → 保留
    TIER2 命中 且 (价格信号 或 工具信号 或 金额) → 保留
    """
    if not text or not text.strip():
        return FilterVerdict(keep=False, tier=None, matched=[], evidence=[])

    lowered = text.lower()

    hit1 = phrases.match_tier1(lowered)
    if hit1:
        return FilterVerdict(keep=True, tier=1, matched=hit1, evidence=[])

    hit2 = phrases.match_tier2(lowered)
    if hit2:
        evidence = phrases.match_price(lowered)
        evidence += phrases.match_tool(lowered)
        if _DOLLAR_RE.search(lowered):
            evidence.append("金额")
        elif _CNY_RE.search(lowered):
            evidence.append("金额")
        if evidence:
            return FilterVerdict(keep=True, tier=2, matched=hit2, evidence=evidence)
        return FilterVerdict(keep=False, tier=2, matched=hit2, evidence=[])

    return FilterVerdict(keep=False, tier=None, matched=[], evidence=[])
