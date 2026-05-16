from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_CJK_NAME = r"[\u4e00-\u9fff]{2,4}"
_QUOTE_OPEN = "“\"「『"
_QUOTE_CLOSE = "”\"」』"
_NON_NAME_WORDS = {
    "母亲",
    "父亲",
    "名字",
    "声音",
    "遗书",
    "的遗书",
    "系统",
    "账本",
    "回声账",
    "回声账本",
    "首席架构",
    "原型设计",
    "设计者之",
    "设计协议",
}
_NON_PERSON_NAME_KEYWORDS = (
    "系统",
    "账本",
    "集团",
    "实验室",
    "记忆",
    "档案",
    "遗书",
    "原型",
    "设计",
    "架构",
    "协议",
    "首席",
    "核心",
    "项目",
    "成员",
    "负责人",
    "失踪",
    "删除",
    "留下",
    "最后",
    "出现",
    "消失",
    "申请",
    "外包",
    "之间",
    "可能",
    "约定",
    "神秘",
    "工位",
    "工作",
    "暗号",
    "关系",
    "录音",
    "音频",
    "视频",
    "语音",
    "文件",
    "结束",
    "警告",
    "区域",
    "残骸",
    "曲线",
    "真相",
    "数据",
    "备份",
    "签署",
    "签名",
    "授权",
    "姓名",
    "名字",
    "本人",
    "呼吸",
    "康复",
    "手术",
    "找到",
    "看到",
    "知道",
    "参与",
    "吻合",
    "坐",
    "下属",
    "你",
    "他",
    "她",
    "不是",
    "普通",
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
_NON_NAME_START_CHARS = ("的", "署", "是", "在", "和", "与", "把", "被", "从", "向", "为")
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


def extract_canon_name_anchors(texts: Iterable[str]) -> list[CanonNameAnchor]:
    anchors: list[CanonNameAnchor] = []
    seen: set[tuple[str, str]] = set()
    patterns = [
        re.compile(
            rf"原型设计者[:：]\s*[{re.escape(_QUOTE_OPEN)}]?({_CJK_NAME})"
            rf"[{re.escape(_QUOTE_CLOSE)}]?[，,]?(?:即|就是)母亲(?:的名字)?"
        ),
        re.compile(
            rf"({_CJK_NAME})[{re.escape(_QUOTE_CLOSE)}]?[，,]?(?:即|就是)母亲(?:的名字)?"
        ),
        re.compile(
            rf"母亲(?:的名字)?(?:是|叫|名为|署名为|署名是)[:：]?"
            rf"[{re.escape(_QUOTE_OPEN)}]?({_CJK_NAME})"
        ),
    ]
    for raw in texts:
        text = str(raw or "").strip()
        if not text:
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                name = _clean_name(match.group(1))
                if not _looks_like_person_name(name):
                    continue
                key = ("母亲", name)
                if key in seen:
                    continue
                seen.add(key)
                anchors.append(
                    CanonNameAnchor(
                        role_label="母亲",
                        canonical_name=name,
                        source_text=_evidence_window(text, match.start(), match.end()),
                    )
                )
    return anchors


def find_canon_name_violations(
    text: str,
    anchors: Iterable[CanonNameAnchor],
) -> list[CanonNameViolation]:
    body = str(text or "")
    if not body:
        return []
    violations: list[CanonNameViolation] = []
    seen: set[tuple[str, str, str]] = set()
    for anchor in anchors:
        role = str(anchor.role_label or "").strip()
        canonical = _clean_name(anchor.canonical_name)
        if role != "母亲" or not _looks_like_person_name(canonical):
            continue

        expanded_pattern = re.compile(
            rf"{re.escape(canonical)}(?P<suffix>[\u4e00-\u9fff]{{1,2}})(?![\u4e00-\u9fff])"
        )
        for match in expanded_pattern.finditer(body):
            observed = _clean_name(match.group(0))
            if observed == canonical or not _looks_like_expanded_person_name(canonical, observed):
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

        for pattern in _mother_name_observation_patterns():
            for match in pattern.finditer(body):
                observed = _clean_name(match.group("name"))
                if not _looks_like_person_name(observed) or observed == canonical:
                    continue
                _append_violation(
                    violations,
                    seen,
                    role=role,
                    canonical=canonical,
                    observed=observed,
                    evidence=_evidence_window(body, match.start(), match.end()),
                    reason="role_name_replacement",
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


def _mother_name_observation_patterns() -> list[re.Pattern[str]]:
    prefix = rf"(?:你(?:的)?|他(?:的)?|她(?:的)?|{_CJK_NAME}(?:的)?)?母亲"
    return [
        re.compile(
            rf"{prefix}(?:叫|名叫|名字是|的名字是|名为|署名为|署名是)[:：]?"
            rf"[{re.escape(_QUOTE_OPEN)}]?(?P<name>{_CJK_NAME})"
        ),
        re.compile(
            rf"{prefix}(?!的)[，,、\s]*[{re.escape(_QUOTE_OPEN)}]?"
            rf"(?P<name>{_CJK_NAME})[{re.escape(_QUOTE_CLOSE)}]?"
            rf"(?=的|是|，|、|曾|在|十年前|授权|[—-]|（|\()"
        ),
        re.compile(
            rf"{prefix}[\s\S]{{0,100}}(?:签名栏|签名|署名|授权书|遗书|协议)"
            rf"[^。！？!?]{{0,45}}(?:名字|姓名|署名)?[:：]\s*"
            rf"[{re.escape(_QUOTE_OPEN)}]?(?P<name>{_CJK_NAME})"
            rf"[{re.escape(_QUOTE_CLOSE)}]?"
            rf"(?=$|[\s，,。；;、！？!?]|的|是|曾|在|十年前|授权)"
        ),
        re.compile(
            rf"(?:你(?:的)?|他(?:的)?|她(?:的)?|{_CJK_NAME}(?:的)?)?"
            rf"母亲(?:的名字|姓名|的姓名|的签名|签名)"
            rf"[\s，,：:、—-]+[{re.escape(_QUOTE_OPEN)}]?"
            rf"(?P<name>{_CJK_NAME})[{re.escape(_QUOTE_CLOSE)}]?"
            rf"(?=$|[\s，,。；;、！？!?])"
        ),
        re.compile(
            rf"(?P<name>{_CJK_NAME})"
            rf"[\s，,。；;、！？!?—-]{{0,12}}"
            rf"(?:{_CJK_NAME}(?:的)?|你(?:的)?|他(?:的)?|她(?:的)?)?"
            rf"母亲(?:的名字|的签名|签名)"
        ),
        re.compile(
            rf"(?:名字|姓名|署名|签名|算法签名|解析结果)"
            rf"[^。！？!?]{{0,45}}[:：]\s*"
            rf"[{re.escape(_QUOTE_OPEN)}]?(?P<name>{_CJK_NAME})"
            rf"[{re.escape(_QUOTE_CLOSE)}]?"
            rf"[\s，,。；;、！？!?]{{0,8}}"
            rf"(?:{_CJK_NAME}的|你(?:的)?|他(?:的)?|她(?:的)?)?母亲"
        ),
    ]


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
    if text in _NON_NAME_WORDS:
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


def _looks_like_expanded_person_name(canonical: str, observed: str) -> bool:
    clean_canonical = _clean_name(canonical)
    clean_observed = _clean_name(observed)
    if not clean_observed.startswith(clean_canonical):
        return False
    suffix = clean_observed[len(clean_canonical) :]
    if not suffix or suffix.startswith(_NON_EXPANDED_NAME_SUFFIX_STARTS):
        return False
    return _looks_like_person_name(clean_observed)


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
