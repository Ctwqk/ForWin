from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Collection, Iterable


_CJK_NAME = r"[\u4e00-\u9fff]{2,4}"
_QUOTE_OPEN = "“\"「『"
_QUOTE_CLOSE = "”\"」』"
# Closed-class function words (pronouns, copula/negation, modals, localizers) that never
# belong to a person name. Content words are deliberately absent: which words can follow
# a canonical name in a given book is not something runtime code may enumerate.
_NON_PERSON_NAME_KEYWORDS = (
    "你",
    "他",
    "她",
    "不是",
    "之间",
    "可能",
    "本人",
)
_COMMON_SINGLE_CHAR_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍"
    "史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元"
    "卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋庞熊"
    "纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐"
    "邱骆高夏蔡田胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁"
    "单杭洪包诸左石崔吉龚程邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳"
    "汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁"
    "仇栾暴甘斜厉戎祖武符刘景詹龙叶幸司韶黎蓟薄印宿白怀蒲邰从鄂"
    "索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰"
    "雍郤璩桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎充慕连茹习宦"
    "艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东殴殳"
    "沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须"
    "丰巢关蒯相查后荆红游竺权逯盖益桓公"
)
_COMMON_COMPOUND_SURNAMES = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "东门", "西门",
)
_NON_NAME_START_CHARS = ("的", "是", "在", "和", "与", "把", "被", "从", "向", "为")
_NON_NAME_END_CHARS = ("的", "后", "前", "时", "中", "里", "上", "下", "在")
_NON_EXPANDED_NAME_SUFFIX_STARTS = (
    "的",
    "是",
    "在",
    "和",
    "与",
    "把",
    "被",
    "说",
    "对",
    "从",
    "向",
    "为",
    "了",
    "话",
    "名",
    "字",
    "还",
    "会",
    "将",
    "已",
)


@dataclass(frozen=True)
class CanonNameAnchor:
    role_label: str
    canonical_name: str
    source_text: str = ""


@dataclass(frozen=True)
class CanonNameViolation:
    role_label: str
    canonical_name: str
    observed_name: str
    evidence: str
    reason: str


def find_canon_name_violations(
    anchors: Iterable[CanonNameAnchor],
    text: str,
    *,
    known_names: Collection[str] = (),
) -> list[CanonNameViolation]:
    """Report names in ``text`` that extend an anchored canonical name (``甲乙`` -> ``甲乙丙``).

    Every anchor is treated alike whatever its ``role_label``; the anchors themselves come
    from the caller (book state), never from patterns over prose.
    Exact matches to known character names or aliases are not drift.
    """
    body = str(text or "")
    if not body:
        return []
    known_name_set = set(known_names)
    violations: list[CanonNameViolation] = []
    seen: set[tuple[str, str, str]] = set()
    for anchor in anchors:
        role = str(anchor.role_label or "").strip()
        canonical = _clean_name(anchor.canonical_name)
        if not role or not canonical:
            continue
        expanded_pattern = re.compile(
            rf"{re.escape(canonical)}(?P<suffix>[\u4e00-\u9fff]{{1,2}})(?![\u4e00-\u9fff])"
        )
        for match in expanded_pattern.finditer(body):
            observed = _clean_name(match.group(0))
            if (
                observed == canonical
                or observed in known_name_set
                or not _looks_like_expanded_name(canonical, observed)
            ):
                continue
            _append_violation(
                violations,
                seen,
                role=role,
                canonical=canonical,
                observed=observed,
                evidence=_evidence_window(body, match.start(), match.end()),
                reason="expanded_canonical_name",
            )
    return violations


def canon_name_anchor_lines(anchors: Iterable[CanonNameAnchor]) -> list[str]:
    return [
        f"{anchor.role_label}姓名：{anchor.canonical_name}"
        for anchor in anchors
        if str(anchor.role_label or "").strip() and str(anchor.canonical_name or "").strip()
    ]


def is_plausible_person_name(name: str) -> bool:
    return _looks_like_person_name(name)


def extract_candidate_character_names(text: str) -> set[str]:
    """Extract likely person names for context assembly only, not canon verdicts."""
    content = str(text or "")
    if not content:
        return set()
    names: set[str] = set()
    patterns = [
        re.compile(r"([\u4e00-\u9fff]{2,4})(?:说|问|答|喊|低声|抬头|转身|点头|摇头)"),
        re.compile(r"(?:叫|名叫|名字是|名为)([\u4e00-\u9fff]{2,4})"),
        re.compile(r"(?:若为|如果为|如果是|身份为|姓名为|实名为)([\u4e00-\u9fff]{2,4})"),
        re.compile(r"(?:直接使用|使用)(?:该)?姓名[：:]?([\u4e00-\u9fff]{2,4})"),
        re.compile(r"([\u4e00-\u9fff]{2,4})(?:和|与)([\u4e00-\u9fff]{2,4})"),
        re.compile(r"(?:救出|救下|释放|解救|营救)(?:被[\u4e00-\u9fff]{0,6}的)?([\u4e00-\u9fff]{2,4})"),
        re.compile(r"(?:被关押的|被扣押的|被捕的|被束缚的)([\u4e00-\u9fff]{2,4})"),
    ]
    for pattern in patterns:
        for match in pattern.finditer(content):
            for group in match.groups():
                candidate = _clean_name(group)
                if _looks_like_person_name(candidate):
                    names.add(candidate)
    return names


def _clean_name(value: str) -> str:
    return str(value or "").strip().strip(f"{_QUOTE_OPEN}{_QUOTE_CLOSE}：:，,。；; ")


def _looks_like_person_name(name: str) -> bool:
    text = _clean_name(name)
    if not _has_common_chinese_name_prefix(text):
        return False
    if text.startswith(_NON_NAME_START_CHARS):
        return False
    if text.endswith(_NON_NAME_END_CHARS):
        return False
    if any(word in text for word in _NON_PERSON_NAME_KEYWORDS):
        return False
    return bool(re.fullmatch(_CJK_NAME, text))


def _has_common_chinese_name_prefix(text: str) -> bool:
    if not text:
        return False
    if any(text.startswith(surname) and len(text) > len(surname) for surname in _COMMON_COMPOUND_SURNAMES):
        return True
    return text[0] in _COMMON_SINGLE_CHAR_SURNAMES


def _looks_like_expanded_name(canonical: str, observed: str) -> bool:
    """True if ``observed`` reads as ``canonical`` plus extra name characters.

    ``canonical`` is a trusted anchor, so only the added suffix is judged.
    """
    if not observed.startswith(canonical):
        return False
    suffix = observed[len(canonical) :]
    if not suffix or suffix.startswith(_NON_EXPANDED_NAME_SUFFIX_STARTS):
        return False
    if suffix.endswith(_NON_NAME_END_CHARS):
        return False
    if any(word in suffix for word in _NON_PERSON_NAME_KEYWORDS):
        return False
    return bool(re.fullmatch(_CJK_NAME, observed))


def _evidence_window(text: str, start: int, end: int, *, radius: int = 22) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right].strip()


def _append_violation(
    violations: list[CanonNameViolation],
    seen: set[tuple[str, str, str]],
    *,
    role: str,
    canonical: str,
    observed: str,
    evidence: str,
    reason: str,
) -> None:
    key = (role, canonical, observed)
    if key in seen:
        return
    seen.add(key)
    violations.append(
        CanonNameViolation(
            role_label=role,
            canonical_name=canonical,
            observed_name=observed,
            evidence=evidence,
            reason=reason,
        )
    )
