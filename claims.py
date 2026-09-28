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
# A leadership word is supported only by itself or its own translation: "managed a class
# project" does not support "led the engineering organization".
LEADERSHIP_GROUPS = (
    ("led", "lead", "leading", "带领", "领导", "主导", "牵头"),
    ("managed", "manage", "managing"),
    ("owned", "owning", "responsible for", "负责"),
    ("spearheaded", "主导", "牵头"),
    ("headed", "领导"),
    ("directed", "领导", "统筹"),
    ("orchestrated", "统筹"),
)
# Words that claim more scale, reach or real-world use than a source lacking them in every
# language: "used by 2 testers" is not "used by 2 million customers".
SCALE_TERMS = (
    ("million", "millions", "百万"),
    ("billion", "billions", "十亿"),
    ("hundred million", "亿"),
    ("thousand", "thousands"),
    ("production", "生产环境"),
    ("customer", "customers", "客户"),
    ("client", "clients", "客户"),
    ("enterprise", "企业级"),
    ("organization", "organisation"),
    ("company-wide", "全公司"),
    ("global", "globally", "worldwide", "全球"),
    ("large-scale", "大规模"),
    ("revenue", "营收", "收入"),
    ("profit", "利润"),
)
# Qualifiers a rewrite must keep in some language: without them, help reads as ownership
# and a course prototype as finished work.
QUALIFIERS = (
    ("prototype", "prototypes", "原型"),
    ("proof of concept", "proof-of-concept", "POC", "概念验证"),
    ("demo", "demos", "demonstration", "演示"),
    ("course", "coursework", "class project", "课程"),
    ("assisted", "assist", "helped", "help", "协助", "辅助", "帮助"),
    ("contributed", "contribute", "contributing", "参与"),
    ("partially", "partial", "部分"),
    ("studied", "学习"),
    ("in progress", "ongoing", "进行中", "在读", "修读中"),
    ("planned", "计划"),
    ("internal", "in-house", "内部"),
)
NEGATION_EN = re.compile(r"\b(?:not|never|no|without|none|cannot)\b|n't\b", re.IGNORECASE)
NOT_NEGATION = re.compile(r"\bnot (?:only|just|merely|simply)\b", re.IGNORECASE)  # "not only built but…"
NEGATION_WORDS = {"not", "never", "no", "without", "none", "cannot"}
NEGATION_HELPERS = {"be", "been", "being", "have", "has", "had", "to", "a", "an", "the", "any", "yet", "ever", "even"}
NEGATION_SCOPE = 6  # words after a negation, within its clause, that it can apply to
# Words that end what a negation applies to: "did not test but deployed" negates only "test".
NEGATION_ENDS = {"but", "and", "while", "whereas", "although", "though", "however", "instead", "then",
                 "so", "because", "after", "before"}  # "yet" is a helper: "not yet deployed"
NEGATION_ENDS_ZH_WORDS = "但|而|却|并且|且|然后|以及|同时|虽然|不过|可是"
NEGATION_ENDS_ZH = re.compile(rf"[，。；、,.;:：!?！？\s]|{NEGATION_ENDS_ZH_WORDS}")
LEADING_ZH = re.compile(r"^(?:[了过有]|同时)+")  # 没有同时部署: 同时 belongs to the negation
CLAUSE_END = re.compile(r"[,.;:!?，。；：！？]")
# A source counts as negated only on clear words; a rewrite keeps the negation with any of
# these characters, so a correct translation is never rejected for wording it differently.
NEGATION_ZH = ("没有", "并未", "尚未", "从未", "未曾", "未能", "并非", "不是", "无法", "不会", "不能", "不再", "未", "不")
NEGATION_ZH_ANY = "不没未无非勿别"
CJK = re.compile(r"[\u4e00-\u9fff]")
# Compounds that contain a listed word without its meaning: 机器学习 is not "studied".
COMPOUNDS = {
    "学习": ("机器学习", "深度学习", "强化学习", "迁移学习", "监督学习", "联邦学习"),
    "未": ("未来", "未知"),
    "不": ("不断", "不同", "不仅", "不少", "不久", "不错", "不但", "不过", "不管", "不论", "不如", "不止", "不只", "不光"),
}
QUANTITY = re.compile(r"(?<![A-Za-z0-9.,])(\d+(?:[.,]\d+)*)")
FOLLOWING_WORDS = re.compile(r"(?:\s+|-)([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,2})")
NOT_UNITS = {
    "a", "an", "and", "the", "or", "to", "of", "in", "on", "at", "for", "with", "by", "from",
    "per", "than", "more", "over", "about", "across", "into", "plus", "that", "which", "who",
}
TIME_UNITS = {"second", "minute", "hour", "day", "week", "month", "quarter", "semester", "year"}
TIME_UNITS_ZH = ("秒", "分钟", "小时", "天", "日", "周", "星期", "个月", "月", "季度", "学期", "年")
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


def _present(term: str, text: str) -> bool:
    for compound in COMPOUNDS.get(term, ()):
        text = text.replace(compound, "")
    return _mentioned(term, text)


def _stated(group: tuple[str, ...], text: str) -> str | None:
    """The first term of the group that the text states, if any."""
    return next((term for term in group if _present(term, text)), None)


def _leadership_supported(word: str, source_text: str) -> bool:
    groups = [group for group in LEADERSHIP_GROUPS if word.casefold() in (term.casefold() for term in group)]
    return any(_stated(group, source_text) for group in groups or [(word,)])


def _negated(text: str, clearly: bool) -> bool:
    if NEGATION_EN.search(NOT_NEGATION.sub("", text)):
        return True
    if clearly:
        return any(_present(word, text) for word in NEGATION_ZH)
    return any(character in text for character in NEGATION_ZH_ANY)


def _stem(word: str) -> str:
    """deploy, deployed, deploying and deployment all become "deploy"."""
    for suffix in ("ment", "ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)]
    return word


def _english_negations(text: str) -> list[tuple[str, str, set[str]]]:
    """Each English negation as (stem of the word it applies to, that word, stems of the words
    after it in its clause): "did not deploy it" -> ("deploy", "deploy", {"deploy", "it"}). The
    clause lets "No third-party libraries were used" still negate "use"."""
    found = []
    for clause in CLAUSE_END.split(NOT_NEGATION.sub("", text)):
        words = [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z'-]*", clause)]
        for index, word in enumerate(words):
            if word in NEGATION_WORDS or word.endswith("n't"):
                following = []
                for later in words[index + 1:]:
                    if later in NEGATION_ENDS:
                        break
                    if later not in NEGATION_HELPERS:
                        following.append(later)
                following = following[:NEGATION_SCOPE]
                if following:
                    found.append((_stem(following[0]), following[0], {_stem(later) for later in following}))
    return found


def _chinese_negations(text: str, clearly: bool) -> list[tuple[str, str]]:
    """Each Chinese negation as (the two characters it applies to, the five after it):
    没有部署服务 -> ("部署", "部署服务"). A source counts only clear negation words."""
    found = []
    for word in NEGATION_ZH if clearly else tuple(NEGATION_ZH_ANY):
        cleaned = text
        for compound in COMPOUNDS.get(word, ()):
            cleaned = cleaned.replace(compound, "")
        for match in re.finditer(re.escape(word), cleaned):
            after = NEGATION_ENDS_ZH.split(LEADING_ZH.sub("", cleaned[match.end():]))[0]
            if after:
                found.append((after[:2], after[:5]))
    return found


def _chinese_names(stem: str) -> set[str]:
    """What the glossary calls an English word in Chinese: deploy -> 部署."""
    return {name for term, others in _OTHER_NAMES.items() if term.isascii() and _stem(term) == stem
            for name in others if not name.isascii()}


def _english_stems(scope: str) -> set[str]:
    """What the glossary calls the Chinese words starting a negated span in English: 部署服务 -> deploy."""
    terms = [term for term in _OTHER_NAMES if not term.isascii() and scope.startswith(term)]
    return {_stem(name.casefold()) for term in terms for name in _OTHER_NAMES[term] if name.isascii()}


def _negations_dropped(text: str, source_text: str) -> list[str]:
    """Negated words of the source that the rewrite no longer negates. Each word must stay
    negated in the same language; in a translation, a word the glossary knows must be negated
    under its translation; otherwise any negation in the translation is accepted."""
    english, chinese = _english_negations(source_text), _chinese_negations(source_text, clearly=True)
    if not english and not chinese:
        return [] if _negated(text, clearly=False) else ["否定"]
    kept_english = [scope for _, _, scope in _english_negations(text)]
    kept_chinese = [scope for _, scope in _chinese_negations(text, clearly=False)]
    in_chinese = CJK.search(text) is not None
    missing = []
    for stem, word, _ in english:
        names = _chinese_names(stem)
        if any(stem in scope for scope in kept_english) or (in_chinese and (
                any(name in scope for scope in kept_chinese for name in names) if names else kept_chinese)):
            continue
        missing.append(word)
    for head, scope in chinese:
        stems = _english_stems(scope)
        if any(head in kept for kept in kept_chinese) or (not in_chinese and (
                any(stem in kept for kept in kept_english for stem in stems) if stems else kept_english)):
            continue
        missing.append(head)
    return list(dict.fromkeys(missing))


def _quantities(text: str) -> list[tuple[str, str | None, str]]:
    """Each number with the English noun it counts (singular, or None) and its kind: time,
    percent or count. "3 backend services in 2 calendar weeks" -> (3, service, count), (2, week, time)."""
    found = []
    for match in QUANTITY.finditer(text):
        rest = text[match.end():]
        noun, kind = None, "count"
        if rest.startswith(("%", "％")):
            noun, kind = "%", "percent"
        elif following := FOLLOWING_WORDS.match(rest):
            words = []
            for word in following.group(1).split():
                word = word.casefold()
                if word in NOT_UNITS:
                    break
                words.append(word)
            if words:
                noun = words[-1]
                noun = noun[:-3] + "y" if noun.endswith("ies") and len(noun) > 4 else (
                    noun[:-1] if noun.endswith("s") and not noun.endswith("ss") and len(noun) > 3 else noun)
                kind = "time" if noun in TIME_UNITS else "count"
        elif rest.lstrip().startswith(TIME_UNITS_ZH):
            kind = "time"
        found.append((match.group(1), noun, kind))
    return found


def _swapped(text: str, source_text: str) -> list[str]:
    """Numbers moved onto something else the source counts: "2 services in 3 weeks" ->
    "3 services in 2 weeks", or in Chinese "2 周内完成 3 个服务" (a time and a count trade places)."""
    source = _quantities(source_text)
    swapped = []
    for number, noun, kind in _quantities(text):
        by_noun = noun is not None and (number, noun) not in {(n, x) for n, x, _ in source} and (
            any(n == number and x for n, x, _ in source) and any(x == noun and n != number for n, x, _ in source))
        by_kind = (number, kind) not in {(n, k) for n, _, k in source} and (
            any(n == number for n, _, _ in source) and any(k == kind and n != number for n, _, k in source))
        if by_noun or by_kind:
            swapped.append(f"{number} {noun}" if noun else number)
    return list(dict.fromkeys(swapped))


def _added_terms(groups: tuple[tuple[str, ...], ...], text: str, source_text: str) -> list[str]:
    """Terms the rewrite states that the source supports under none of the groups holding them."""
    added = []
    for group in groups:
        term = _stated(group, text)
        if term and term not in added and not any(_stated(other, source_text) for other in groups if term in other):
            added.append(term)
    return added


def _dropped_terms(groups: tuple[tuple[str, ...], ...], text: str, source_text: str) -> list[str]:
    """Terms the source states that the rewrite keeps under none of the groups holding them."""
    dropped = []
    for group in groups:
        term = _stated(group, source_text)
        if term and term not in dropped and not any(_stated(other, text) for other in groups if term in other):
            dropped.append(term)
    return dropped


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
    claimed = [word for word in dict.fromkeys(_leadership(text)) if not _leadership_supported(word, source_text)]
    if claimed:
        reasons.append(f"表述强度超过原事实：{', '.join(claimed)}")
    for number in sorted(_numbers(text) - _numbers(source_text)):
        reasons.append(f"原事实中没有这个数字：{number}")
    for pair in _swapped(text, source_text):
        reasons.append(f"数字对应的内容与原事实不同：{pair}")
    for term in _added_terms(SCALE_TERMS, text, source_text):
        reasons.append(f"表述范围超过原事实：{term}")
    for term in _dropped_terms(QUALIFIERS, text, source_text):
        reasons.append(f"改写去掉了原事实中的限定：{term}")
    if _negated(source_text, clearly=True):
        dropped = _negations_dropped(text, source_text)
        if dropped:
            reasons.append(f"改写去掉了原事实中的否定：{', '.join(dropped)}")
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
