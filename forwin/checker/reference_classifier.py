from __future__ import annotations

import re

GENERIC_CHARACTER_REFERENCES = {
    "路人",
    "守卫",
    "老板",
    "店小二",
    "师兄",
    "师姐",
    "弟子",
    "首席运营官",
    "运营负责人",
    "财务总监",
    "财务负责人",
    "法务负责人",
    "部门总监",
    "部门负责人",
    "集团高管",
    "同学",
    "众人",
    "人群",
    "旁人",
    "馆员",
    "管理员",
    "工作人员",
    "服务员",
    "追踪者",
    "不明追踪者",
    "无脸人",
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
    "权限买家",
    "权限卖家",
    "权限者",
    "代理人",
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
GENERIC_CHARACTER_ROLE_SUFFIXES = (
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
    "技术员",
    "工程师",
    "程序员",
    "黑客",
    "线人",
    "中间人",
    "摊主",
    "追兵",
    "追踪者",
    "安保",
    "保镖",
    "警员",
    "警察",
    "巡检员",
    "员工",
    "主管",
    "买家",
    "卖家",
    "权限买家",
    "权限卖家",
    "权限者",
    "女孩",
    "男孩",
    "儿童",
    "小孩",
    "代理人",
    "持有者",
    "残影",
    "调度员",
    "尸体",
    "遗体",
    "躯体",
    "死者",
    "遇难者",
    "遗骸",
)
POSSESSIVE_GENERIC_ROLE_SUFFIXES = (
    "手下",
    "下属",
    "部下",
    "同伙",
    "随从",
    "队员",
    "巡检员",
    "追兵",
    "追踪者",
    "守卫",
    "保镖",
    "安保",
    "员工",
)
ROLE_PREFIXED_PERSON_NAME_PREFIXES = (
    "馆员",
    "审计员",
    "调度员",
    "接线员",
    "管理员",
    "工程师",
)
NON_CHARACTER_NAME_KEYWORDS = (
    "集团",
    "公司",
    "机构",
    "报社",
    "系统",
    "账本",
    "记忆馆",
    "旧港",
    "火灾",
    "事故",
    "码头",
    "咖啡馆",
    "档案",
    "论坛",
    "市场",
    "大楼",
    "实验室",
    "实验区",
)
NON_CHARACTER_NAME_SUFFIXES = (
    "宗邦",
    "董事会",
    "委员会",
    "档案署",
    "审计局",
    "管理局",
)
RELATIONAL_REFERENCE_SUFFIXES = (
    "母亲",
    "父亲",
    "妈妈",
    "爸爸",
    "姐姐",
    "妹妹",
    "哥哥",
    "弟弟",
    "的母亲",
    "的父亲",
    "的妈妈",
    "的爸爸",
    "的姐姐",
    "的妹妹",
    "的哥哥",
    "的弟弟",
)
TECHNICAL_ID_RE = re.compile(
    r"^(?=.*(?:[A-Za-zＡ-Ｚａ-ｚ]|[0-9０-９]))"
    r"[A-Za-zＡ-Ｚａ-ｚ0-9０-９]+"
    r"(?:[-_][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]+)+$"
)
ROLE_NUMBERED_ID_RE = re.compile(
    r"^[\u4e00-\u9fff]{1,8}[-_－—][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]{1,8}$"
)
PREFIXED_TECHNICAL_ID_RE = re.compile(
    r"^(?P<prefix>[\u4e00-\u9fff]{1,10})"
    r"(?P<identifier>[A-Za-zＡ-Ｚａ-ｚ][A-Za-zＡ-Ｚａ-ｚ0-9０-９]*"
    r"[-_－—][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]{1,8}"
    r"(?:[-_－—][A-Za-zＡ-Ｚａ-ｚ0-9０-９γΩαβ]{1,8})*)$"
)
TECHNICAL_ID_ROLE_PREFIX_SUFFIXES = (
    "载体",
    "审计员",
    "镜像审计员",
    "节点",
    "分割体",
)
CJK_NUMBER_CHARS = "零〇一二三四五六七八九十百千万两"
NUMBERED_PLOT_ENTITY_RE = re.compile(
    rf"^(?:第)?(?:[0-9０-９]{{1,4}}|[{CJK_NUMBER_CHARS}]{{1,8}})(?:号|份|枚)"
    r"(?:锚点|分割体|密钥|碎片|样本|载体|节点|密钥持有者|碎片持有者)$"
)
ANCHOR_ID_RE = re.compile(r"^锚点[0-9０-９]{1,4}$")
COMPOUND_IDENTITY_RE = re.compile(r"^[\u4e00-\u9fff·]{2,6}(?:/|与)[\u4e00-\u9fff·]{2,6}$")
COMPOUND_PERSONA_PAREN_RE = re.compile(r"^[\u4e00-\u9fff·]{2,6}[（(][\u4e00-\u9fff·]{2,8}人格[）)]$")
STATUS_LABEL_RE = re.compile(r"^[\u4e00-\u9fff]{1,8}(?:[-_－—][\u4e00-\u9fff]{1,8})+$")
STATUS_LABEL_SUFFIXES = {
    "活跃",
    "已故",
    "在线",
    "离线",
    "冻结",
    "失效",
    "待审",
    "复核中",
    "未知",
    "匿名",
}
PARENTHETICAL_REFERENCE_LABELS = {
    "提及",
    "无名",
    "记录",
    "旁白",
    "幕后",
    "间接",
    "残留影像",
    "AI备份",
    "体内含蘅照夜意识",
    "镜像投影",
    "远程信号压力",
    "远程追踪信号",
    "远程声音",
}
PARENTHETICAL_REFERENCE_LABEL_KEYWORDS = (
    "远程",
    "信号",
    "声音",
    "录音",
    "投影",
    "影像",
    "备份",
    "副本",
    "意识",
)


def has_malformed_parenthetical_annotation(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    for opener, closer in (("（", "）"), ("(", ")")):
        if text.count(opener) != text.count(closer):
            return True
        if opener in text and closer in text and text.rfind(opener) > text.rfind(closer):
            return True
    return False


def looks_like_parenthetical_reference_label(label: str) -> bool:
    text = str(label or "").strip()
    if text in PARENTHETICAL_REFERENCE_LABELS:
        return True
    if not (2 <= len(text) <= 18):
        return False
    if any(char in text for char in "（）()"):
        return False
    return any(keyword in text for keyword in PARENTHETICAL_REFERENCE_LABEL_KEYWORDS)


def normalize_character_reference(name: str) -> str:
    text = str(name or "").strip()
    for opener, closer in (("（", "）"), ("(", ")")):
        if opener not in text or not text.endswith(closer):
            continue
        prefix, suffix = text.rsplit(opener, 1)
        suffix = suffix[: -len(closer)].strip()
        prefix = prefix.strip()
        if looks_like_parenthetical_reference_label(suffix) and prefix:
            text = prefix
        elif prefix and looks_like_generic_character_reference(prefix):
            text = prefix
    text = normalize_mixed_technical_alias(text)
    return strip_role_prefix_from_person_name(text)


def normalize_mixed_technical_alias(name: str) -> str:
    text = str(name or "").strip()
    for separator in ("/", "／"):
        if separator not in text:
            continue
        left, right = (part.strip() for part in text.split(separator, 1))
        if looks_like_technical_identifier(right) and (
            is_plain_chinese_person_name(left) or looks_like_named_alias_base(left)
        ):
            return left
        if looks_like_technical_identifier(left) and (
            is_plain_chinese_person_name(right) or looks_like_named_alias_base(right)
        ):
            return right
        if looks_like_technical_identifier(right) and looks_like_generic_character_reference(left):
            return left
        if looks_like_technical_identifier(left) and looks_like_generic_character_reference(right):
            return right
        if looks_like_generic_character_reference(left) and looks_like_named_alias_base(right):
            return right
    return text


def looks_like_named_alias_base(name: str) -> bool:
    text = str(name or "").strip()
    return (
        2 <= len(text) <= 12
        and "/" not in text
        and "／" not in text
        and any("\u4e00" <= ch <= "\u9fff" for ch in text)
        and not looks_like_technical_identifier(text)
        and not looks_like_generic_character_reference(text)
        and not looks_like_non_character_reference(text)
    )


def strip_role_prefix_from_person_name(name: str) -> str:
    text = str(name or "").strip()
    for prefix in ROLE_PREFIXED_PERSON_NAME_PREFIXES:
        if not text.startswith(prefix) or len(text) <= len(prefix):
            continue
        suffix = text[len(prefix) :].strip()
        if is_plain_chinese_person_name(suffix):
            return suffix
    return text


def is_plain_chinese_person_name(name: str) -> bool:
    text = str(name or "").strip()
    return (
        2 <= len(text) <= 4
        and "的" not in text
        and not any(ch.isdigit() for ch in text)
        and all("\u4e00" <= ch <= "\u9fff" or ch == "·" for ch in text)
        and not looks_like_generic_character_reference(text)
        and not looks_like_non_character_reference(text)
    )


def looks_like_technical_identifier(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    return bool(
        TECHNICAL_ID_RE.fullmatch(text)
        or ROLE_NUMBERED_ID_RE.fullmatch(text)
        or looks_like_prefixed_technical_identifier(text)
    )


def looks_like_prefixed_technical_identifier(name: str) -> bool:
    text = str(name or "").strip()
    match = PREFIXED_TECHNICAL_ID_RE.fullmatch(text)
    if not match:
        return False
    prefix = match.group("prefix")
    return looks_like_generic_character_reference(prefix) or any(
        prefix.endswith(suffix) for suffix in TECHNICAL_ID_ROLE_PREFIX_SUFFIXES
    )


def looks_like_numbered_plot_entity(name: str) -> bool:
    text = str(name or "").strip()
    return bool(NUMBERED_PLOT_ENTITY_RE.fullmatch(text) or ANCHOR_ID_RE.fullmatch(text))


def looks_like_compound_identity(name: str) -> bool:
    text = str(name or "").strip()
    return bool(COMPOUND_IDENTITY_RE.fullmatch(text) or COMPOUND_PERSONA_PAREN_RE.fullmatch(text))


def looks_like_status_label_reference(name: str) -> bool:
    text = str(name or "").strip()
    if not text or not STATUS_LABEL_RE.fullmatch(text):
        return False
    tail = re.split(r"[-_－—]", text)[-1]
    return tail in STATUS_LABEL_SUFFIXES


def looks_like_generic_character_reference(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text in GENERIC_CHARACTER_REFERENCES:
        return True
    if looks_like_numbered_plot_entity(text):
        return True
    if looks_like_technical_identifier(text):
        return True
    if looks_like_compound_identity(text):
        return True
    if looks_like_status_label_reference(text):
        return True
    if "的" in text:
        _prefix, suffix = text.rsplit("的", 1)
        if suffix and any(suffix.endswith(role) for role in POSSESSIVE_GENERIC_ROLE_SUFFIXES):
            return True
    return len(text) <= 8 and any(text.endswith(suffix) for suffix in GENERIC_CHARACTER_ROLE_SUFFIXES)


def looks_like_non_character_reference(name: str) -> bool:
    text = str(name or "").strip()
    if any(text.endswith(suffix) for suffix in RELATIONAL_REFERENCE_SUFFIXES):
        return True
    if any(text.endswith(suffix) for suffix in NON_CHARACTER_NAME_SUFFIXES):
        return True
    return any(keyword in text for keyword in NON_CHARACTER_NAME_KEYWORDS)


def looks_like_named_character(name: str) -> bool:
    text = normalize_character_reference(name)
    if not text or looks_like_generic_character_reference(text):
        return False
    if looks_like_non_character_reference(text):
        return False
    return len(text) <= 12


def candidate_character_name(name: str) -> str:
    raw_text = str(name or "").strip()
    if has_malformed_parenthetical_annotation(raw_text):
        return ""
    text = normalize_character_reference(raw_text)
    return text if looks_like_named_character(text) else ""
