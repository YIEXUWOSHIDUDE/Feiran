"""Deterministic checks that a rewritten CV line claims nothing beyond its source fact."""

import re
from typing import Iterable

from facts import tag_pattern


MAX_LINE_LENGTH = 300
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.+#-][A-Za-z0-9]+)*[+#]*")
LINK = re.compile(r"https?://|www\.|@")
LEADERSHIP_EN = re.compile(
    r"\b(?:led|lead|leading|managed|manage|managing|owned|owning|spearheaded|headed|"
    r"directed|architected|supervised|mentored|founded|drove|championed|pioneered|"
    r"orchestrated|responsible for)\b",
    re.IGNORECASE,
)
LEADERSHIP_ZH = ("主导", "带领", "领导", "牵头", "负责", "统筹")
# Terms a translation may use for words its source line already has, in either direction:
# "linking the frontend" may become "连接前端". A term missing from the source in every
# language is still a new claim.
EQUIVALENTS = (
    ("前端", "frontend", "front-end", "front end"),
    ("后端", "backend", "back-end", "back end"),
    ("接口", "API", "APIs", "interface", "interfaces"),
    ("数据库", "database", "databases"),
    ("测试", "testing", "test", "tests", "tested"),
    ("单元测试", "unit test", "unit tests"),
    ("算法", "algorithm", "algorithms"),
    ("机器学习", "machine learning"),
    ("深度学习", "deep learning"),
    ("大模型", "large language model", "large language models", "LLM", "LLMs"),
    ("重构", "refactor", "refactored", "refactoring"),
    ("文字识别", "OCR", "text recognition"),
    ("持续集成", "continuous integration", "CI"),
    ("计算机", "computer", "computer science"),
    ("论文", "paper", "papers", "publication", "publications"),
    ("硕士", "master", "master's", "M.S."),
    ("学士", "bachelor", "bachelor's", "B.S."),
    ("本科", "bachelor", "bachelor's", "undergraduate", "B.S."),
    ("研究生", "graduate", "master", "master's", "M.S."),
    ("研究", "research"),
    ("调试", "debugging", "debug", "debugged"),
    ("部署", "deployment", "deploy", "deployed"),
    ("实时", "real-time", "realtime"),
    ("预测", "forecasting", "forecast"),
    ("分类", "classification"),
    ("数据交换", "data exchange"),
)
_OTHER_NAMES: dict[str, set[str]] = {}
for _group in EQUIVALENTS:
    for _term in _group:
        _OTHER_NAMES.setdefault(_term.casefold(), set()).update(name for name in _group if name != _term)


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def _leadership(text: str) -> list[str]:
    words = [match.group(0) for match in LEADERSHIP_EN.finditer(text)]
    return words + [word for word in LEADERSHIP_ZH if word in text]


def _mentioned(term: str, text: str) -> bool:
    """The term, or its English singular or plural, appears in the text as a whole word."""
    variants = [term]
    if term.isascii():
        variants.append(term[:-1] if term.endswith("s") else term + "s")
    return any(tag_pattern(variant).search(text) for variant in variants if variant.strip())


def _supported(term: str, source_text: str, tags: set[str]) -> bool:
    """The source line or its own tags already state the term, in this or the other language."""
    names = [term, *_OTHER_NAMES.get(term.casefold(), ())]
    return any(name.casefold() in tags or _mentioned(name, source_text) for name in names)


def _technical(token: str) -> bool:
    """Words shaped like product or technology names: Qwen3-32B, Vue.js, C++, REST, FastAPI."""
    return (
        any(character.isdigit() for character in token)
        or any(character in ".+#" for character in token)
        or sum(character.isupper() for character in token) >= 2
        or re.search(r"[a-z][A-Z]", token) is not None
    )


def check_rewrite(
    text: str,
    source_text: str,
    source_tags: Iterable[str] = (),
    vocabulary: Iterable[str] = (),
) -> list[str]:
    """Return the reasons a rewrite is not supported by its source; empty means it passes.

    ``source_tags`` are the fact's own confirmed tags, which may name a term in another
    language (for example 测试 for testing). ``vocabulary`` is every tag on the CV, so a
    skill from one fact cannot be moved into a line about another.
    """
    if not isinstance(text, str) or not text.strip():
        return ["改写内容为空"]
    reasons: list[str] = []
    if "\n" in text.strip() or "\r" in text:
        reasons.append("改写必须是一行")
    if len(text) > MAX_LINE_LENGTH:
        reasons.append(f"改写超过 {MAX_LINE_LENGTH} 个字符")
    if LINK.search(text) and not LINK.search(source_text):
        reasons.append("改写中不能加入链接或邮箱")
    claimed = _leadership(text)
    if claimed and not _leadership(source_text):
        reasons.append(f"表述强度超过原事实：{', '.join(dict.fromkeys(claimed))}")
    for number in sorted(_numbers(text) - _numbers(source_text)):
        reasons.append(f"原事实中没有这个数字：{number}")
    source = source_text.casefold()
    tags = {tag.casefold() for tag in source_tags}
    for token in dict.fromkeys(WORD.findall(text)):
        if _technical(token) and token.casefold() not in source and token.casefold() not in tags:
            reasons.append(f"原事实中没有这个技术或技能词：{token}")
    for term in dict.fromkeys(vocabulary):
        if tag_pattern(term).search(text) and not _supported(term, source_text, tags):
            reason = f"原事实中没有这个技术或技能词：{term}"
            if reason not in reasons:
                reasons.append(reason)
    return reasons
