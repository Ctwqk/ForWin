from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ReferenceScope = Literal["language_generic", "genre_candidate", "project"]
CatalogScope = Literal["global", "genre_candidate"]


@dataclass(frozen=True, slots=True)
class ReferenceRuleDescriptor:
    rule_key: str
    summary: str
    scope: CatalogScope


@dataclass(frozen=True, slots=True)
class ReferenceClassification:
    scope: ReferenceScope
    features: tuple[str, ...] = ()
    deterministic_generic: bool = False
    deterministic_non_character: bool = False


# Only unambiguous linguistic placeholders may bypass entity admission globally.
LANGUAGE_GENERIC_CHARACTER_REFERENCES = frozenset(
    {
        "路人",
        "守卫",
        "老板",
        "店小二",
        "师兄",
        "师姐",
        "弟子",
        "同学",
        "众人",
        "人群",
        "旁人",
        "管理员",
        "工作人员",
        "服务员",
        "不明追踪者",
        "未知人物",
        "神秘人物",
        "匿名人物",
        "无名人物",
        "陌生人",
        "神秘人",
        "未知者",
        "匿名者",
        "不明人士",
        "另一身份",
        "其他身份",
        "某个身份",
        "某一身份",
        "未知身份",
        "匿名身份",
        "不明身份",
        "未明身份",
        "匿名买家",
        "匿名卖家",
        "女孩",
        "男孩",
        "儿童",
        "小孩",
        "买家",
        "卖家",
        "持有者",
        "手下",
        "下属",
        "部下",
        "同伙",
        "随从",
        "尸体",
        "遗体",
        "躯体",
        "死者",
        "遇难者",
        "遗骸",
    }
)

_POSSESSIVE_GENERIC_ROLE_SUFFIXES = (
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
    "队员",
    "追兵",
    "守卫",
    "保镖",
    "员工",
)
_RELATIONAL_REFERENCE_SUFFIXES = (
    "母亲",
    "父亲",
    "妈妈",
    "爸爸",
    "姐姐",
    "妹妹",
    "哥哥",
    "弟弟",
)
_GENRE_ROLE_SUFFIXES = (
    "技术员",
    "工程师",
    "程序员",
    "黑客",
    "线人",
    "中间人",
    "摊主",
    "追踪者",
    "巡检员",
    "安保",
    "保镖",
    "警员",
    "警察",
    "主管",
    "代理人",
    "残影",
    "调度员",
)
_ORGANIZATION_KEYWORDS = (
    "集团",
    "公司",
    "机构",
    "报社",
    "系统",
    "董事会",
    "委员会",
    "管理局",
    "审计局",
)
_PARENTHETICAL_CANDIDATE_KEYWORDS = (
    "远程",
    "信号",
    "声音",
    "录音",
    "投影",
    "影像",
    "备份",
    "副本",
    "意识",
    "人格",
)

_TECHNICAL_ID_RE = re.compile(
    r"^(?=.*(?:[A-Za-zＡ-Ｚａ-ｚ]|[0-9０-９]))"
    r"[A-Za-zＡ-Ｚａ-ｚ0-9０-９]+"
    r"(?:[-_－—][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]+)+$"
)
_ROLE_NUMBERED_ID_RE = re.compile(
    r"^[\u4e00-\u9fff]{1,8}[-_－—][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]{1,8}$"
)
_COMPOUND_IDENTITY_RE = re.compile(
    r"^[\u4e00-\u9fff·]{2,8}(?:/|／|与)[\u4e00-\u9fff·]{2,8}$"
)
_STATUS_LABEL_RE = re.compile(r"^[\u4e00-\u9fff]{1,8}(?:[-_－—][\u4e00-\u9fff]{1,8})+$")
_STATUS_LABEL_SUFFIXES = frozenset(
    {"活跃", "已故", "在线", "离线", "冻结", "失效", "待审", "复核中"}
)

_RULE_CATALOG = (
    ReferenceRuleDescriptor(
        rule_key="reference.language_generic_exact",
        summary="无歧义语言泛称与匿名占位",
        scope="global",
    ),
    ReferenceRuleDescriptor(
        rule_key="reference.language_generic_possessive_role",
        summary="带所有格的泛称角色",
        scope="global",
    ),
    ReferenceRuleDescriptor(
        rule_key="reference.language_generic_relationship",
        summary="未命名亲属关系引用",
        scope="global",
    ),
    ReferenceRuleDescriptor(
        rule_key="reference.technical_identifier_shape",
        summary="技术标识符形态，仅作为准入候选特征",
        scope="genre_candidate",
    ),
    ReferenceRuleDescriptor(
        rule_key="reference.role_or_organization_shape",
        summary="职业或组织形态，仅作为准入候选特征",
        scope="genre_candidate",
    ),
    ReferenceRuleDescriptor(
        rule_key="reference.identity_annotation_shape",
        summary="复合身份、状态和括注形态，仅作为准入候选特征",
        scope="genre_candidate",
    ),
)


def reference_rule_catalog() -> tuple[ReferenceRuleDescriptor, ...]:
    return _RULE_CATALOG


def classify_reference(name: str) -> ReferenceClassification:
    text = str(name or "").strip()
    if not text:
        return ReferenceClassification(scope="project")
    if text in LANGUAGE_GENERIC_CHARACTER_REFERENCES:
        return ReferenceClassification(
            scope="language_generic",
            features=("exact_generic",),
            deterministic_generic=True,
        )
    if "的" in text:
        _owner, suffix = text.rsplit("的", 1)
        if suffix in _POSSESSIVE_GENERIC_ROLE_SUFFIXES:
            return ReferenceClassification(
                scope="language_generic",
                features=("possessive_generic_role",),
                deterministic_generic=True,
            )
        if suffix in _RELATIONAL_REFERENCE_SUFFIXES:
            return ReferenceClassification(
                scope="language_generic",
                features=("relational_reference",),
                deterministic_non_character=True,
            )

    features: list[str] = []
    if _TECHNICAL_ID_RE.fullmatch(text) or _ROLE_NUMBERED_ID_RE.fullmatch(text):
        features.append("technical_identifier")
    if _COMPOUND_IDENTITY_RE.fullmatch(text):
        features.append("compound_identity")
    if _looks_like_status_label(text):
        features.append("status_label")
    if any(text.endswith(suffix) for suffix in _GENRE_ROLE_SUFFIXES):
        features.append("role_shape")
    if any(keyword in text for keyword in _ORGANIZATION_KEYWORDS):
        features.append("organization_shape")
    if _has_parenthetical_candidate_label(text):
        features.append("identity_annotation")
    if features:
        return ReferenceClassification(
            scope="genre_candidate",
            features=tuple(dict.fromkeys(features)),
        )
    return ReferenceClassification(scope="project")


def _looks_like_status_label(text: str) -> bool:
    if not _STATUS_LABEL_RE.fullmatch(text):
        return False
    return re.split(r"[-_－—]", text)[-1] in _STATUS_LABEL_SUFFIXES


def _has_parenthetical_candidate_label(text: str) -> bool:
    for opener, closer in (("（", "）"), ("(", ")")):
        if opener not in text or not text.endswith(closer):
            continue
        label = text.rsplit(opener, 1)[1][: -len(closer)]
        return any(keyword in label for keyword in _PARENTHETICAL_CANDIDATE_KEYWORDS)
    return False


def looks_like_generic_character_reference(name: str) -> bool:
    return classify_reference(name).deterministic_generic


def looks_like_non_character_reference(name: str) -> bool:
    return classify_reference(name).deterministic_non_character


__all__ = [
    "LANGUAGE_GENERIC_CHARACTER_REFERENCES",
    "ReferenceClassification",
    "ReferenceRuleDescriptor",
    "classify_reference",
    "looks_like_generic_character_reference",
    "looks_like_non_character_reference",
    "reference_rule_catalog",
]
