from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import random
import re
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CultureLexicon:
    display: str
    aliases: tuple[str, ...]
    surnames: tuple[str, ...] = ()
    given: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()
    region_suffixes: tuple[str, ...] = ()
    place_suffixes: tuple[str, ...] = ()
    epithets: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


CULTURES: dict[str, CultureLexicon] = {
    "sinic": CultureLexicon(
        display="中华",
        aliases=("中华", "中式", "华夏", "东方", "礼制", "礼川"),
        surnames=("晏", "阙", "澹", "蘅", "缙", "玄", "珩", "雎", "洛", "衡", "沈", "岑", "祁", "郁", "闻", "谢"),
        given=("砚", "岫", "澜", "昭", "绫", "珂", "璟", "若", "昀", "槐", "潆", "清", "岳", "行", "庭", "照", "微", "阑"),
        titles=("司籍", "典仪", "守仓", "书院生", "坊正", "漕使"),
        roots=("澜", "岫", "衡", "砚", "阙", "蘅", "珩", "潆", "昭", "璟", "昀", "若", "槐", "绫", "珂", "缙", "玄", "雎", "洛", "庭"),
        region_suffixes=("州", "府", "郡", "道", "泽", "川", "岭", "原", "港", "关", "湾", "县"),
        place_suffixes=("城", "坊", "驿", "关", "书院", "祠", "桥", "市", "仓", "台", "坞", "渡", "楼", "苑"),
        epithets=("澜衡诸州", "澜阙诸府", "砚桥礼邦", "水网书院邦", "缙澜漕盟", "玄衡关国", "若槐宗邦"),
        forbidden=("长安", "洛阳", "北京", "南京", "成都", "杭州", "江南", "中原"),
    ),
    "nordic": CultureLexicon(
        display="维京",
        aliases=("维京", "北海", "寒湾", "长船", "北海氏族"),
        given=("斯卡文", "维尔娜", "布伦克", "凯德伦", "索恩雅", "赫温", "伊斯克", "费恩拉", "暮克", "哈尔登", "奥洛克", "盾格"),
        families=("霜帆", "鲸骨", "寒桨", "灰湾", "铁潮", "白桦", "暮海", "符石", "狼桅", "鸦帆", "冰誓", "断浪"),
        titles=("长船主", "誓约人", "符石守", "寒湾领主", "桅楼守"),
        roots=("霜帆", "鲸骨", "寒桨", "灰湾", "铁潮", "白桦", "暮海", "符石", "狼桅", "鸦帆", "冰誓", "断浪", "霜林", "寒湾"),
        region_suffixes=("湾", "岬", "岛", "礁", "船领", "氏族领", "寒峡", "霜港", "冰原", "长屋领"),
        place_suffixes=("长屋", "船坞", "码头", "符石", "烽台", "誓场", "鲸骨台", "集会厅", "寒仓", "桅楼"),
        epithets=("北海氏族", "霜帆诸族", "鲸骨长屋民", "寒湾长船盟", "铁潮霜港民", "灰湾誓约邦", "冰誓海民"),
        forbidden=("奥斯陆", "卑尔根", "雷克雅未克", "乌普萨拉", "奥丁", "索尔", "洛基", "拉格纳"),
    ),
    "roman": CultureLexicon(
        display="罗马",
        aliases=("罗马", "帝国", "石律", "石律帝国", "行省"),
        given=("卡维安", "卢曼娜", "瓦缇奥", "瑟拉文", "科维奥", "阿维伦", "内默", "梅伦提", "托瓦尔", "奥德莉", "帕兰", "维洛斯"),
        families=("石冠", "铜章", "鹰路", "白渠", "红柱", "银秤", "法环", "军靴", "拱门", "灰庭", "铁印", "金阶"),
        titles=("总督", "法务官", "百夫长", "书记官", "公民代表"),
        roots=("卡维", "卢曼", "瓦缇", "瑟拉", "科文", "阿维", "内默", "梅伦", "托瓦", "奥德", "帕兰", "维洛"),
        region_suffixes=("行省", "边省", "石路区", "殖镇", "军镇", "法庭辖", "渠省", "总督辖", "城邦", "税区"),
        place_suffixes=("石门", "广场", "军营", "水渠", "浴场", "法庭", "路驿", "拱门", "税署", "议事厅", "边墙", "柱廊"),
        epithets=("石律帝国", "白渠诸省", "鹰路行省民", "法环公民邦", "铜章军团国", "红柱总督辖", "银秤法典邦"),
        forbidden=("罗马", "凯撒", "奥古斯都", "君士坦丁堡", "庞贝", "亚历山大"),
    ),
    "western": CultureLexicon(
        display="西欧/英国",
        aliases=("西欧", "英国", "英伦", "西欧/英国", "封建", "雾堡", "骑士"),
        given=("布兰", "艾尔妲", "梅洛", "奥伦", "海伦", "格蕾娅", "洛威", "埃德温", "玛洛", "贝伦", "希尔妲", "柯林"),
        families=("棘谷", "白蜡", "灰堡", "鹿沼", "鸦丘", "橡厅", "雾桥", "岩脊", "荆原", "青炉", "黑溪", "旧塔"),
        titles=("骑士", "庄园主", "行会长", "边堡守", "王林巡守"),
        roots=("棘谷", "白蜡", "灰堡", "鹿沼", "鸦丘", "橡厅", "雾桥", "岩脊", "荆原", "青炉", "黑溪", "旧塔", "橡林", "雾沼"),
        region_suffixes=("郡", "侯领", "边侯领", "庄区", "沼地", "荒原", "王林", "采石区", "河口领", "堡辖"),
        place_suffixes=("堡", "磨坊", "集市", "桥", "绿地", "行会厅", "采石场", "哨塔", "庄园", "教堂", "马厩", "渡口"),
        epithets=("雾堡诸领", "棘谷侯邦", "白蜡庄园民", "灰桥骑士领", "橡厅王林盟", "鹿沼封地民", "旧塔行会邦"),
        forbidden=("伦敦", "约克", "牛津", "剑桥", "威塞克斯", "梅西亚", "亚瑟"),
    ),
    "latin": CultureLexicon(
        display="南美/拉丁",
        aliases=("南美", "拉丁", "南美/拉丁", "高原", "雨林", "彩陶"),
        given=("塔维罗", "耶尔卡", "索琳娜", "诺罗", "维莉娅", "夏卢", "帕佐", "托玛", "雅雷", "查维", "奎拉", "纳雅"),
        families=("红陶", "金穗", "盐路", "绿羽", "山鼓", "铜环", "雨湾", "面具", "日庭", "河阶", "彩石", "藤港"),
        titles=("面具祭长", "盐路议员", "彩陶匠首", "梯田长", "河港守"),
        roots=("纳雅", "奎拉", "萨鲁", "雅雷", "查维", "托玛", "夏卢", "维卡", "翁巴", "帕佐", "铜环", "日庭", "藤港", "雨湾"),
        region_suffixes=("谷地", "高原", "盐路区", "河湾", "彩陶州", "梯田领", "雨林边地", "铜矿辖", "港湾领", "日庭辖"),
        place_suffixes=("广场", "梯城", "河港", "彩市", "盐驿", "陶窑", "日庭", "面具厅", "铜矿", "节场", "藤桥", "雨仓"),
        epithets=("彩陶高原民", "日庭梯田邦", "盐路雨林盟", "金穗广场城", "面具河湾民", "铜环山鼓邦", "藤港彩市民"),
        forbidden=("利马", "库斯科", "基多", "波哥大", "安第斯", "亚马孙", "印加"),
    ),
    "church": CultureLexicon(
        display="基督教",
        aliases=("基督", "基督教", "教会", "圣钟", "圣钟教会", "修院"),
        given=("埃洛恩", "瑟雷夫", "奥里安", "梅尔文", "维斯特", "萨隆", "卡莱德", "阿斯瑞尔", "维利斯", "莫里恩", "白烛", "晨辉"),
        titles=("修士", "修女", "主祭", "女院长", "巡礼者", "圣钟书记", "守誓者", "唱诗长", "抄经士", "济贫长"),
        roots=("圣瑟雷夫", "银钟", "烛痕", "奥里安", "梅尔文", "晨辉", "白烛", "莫里恩", "灰钟", "圣维斯特", "阿斯瑞尔"),
        region_suffixes=("教区", "圣辖", "钟领", "修院领", "朝圣道", "烛庭", "主教座", "圣物辖", "白钟区", "济贫辖"),
        place_suffixes=("礼拜堂", "修院", "圣物室", "抄经院", "济贫院", "钟塔", "主教座堂", "朝圣驿", "唱诗厅", "烛庭"),
        epithets=("圣钟教会", "白烛修院民", "晨辉朝圣会", "银钟圣辖", "灰钟抄经会", "烛痕济贫会", "奥里安主教座"),
        forbidden=("耶路撒冷", "伯利恒", "耶稣", "玛利亚", "彼得", "保罗", "约翰"),
    ),
    "crescent": CultureLexicon(
        display="穆斯林",
        aliases=("穆斯林", "伊斯兰", "月穹", "月穹商路", "绿洲", "商路"),
        given=("扎伊鲁", "纳沃克", "哈文", "寇林", "苏维勒", "蕾姆扎", "巴卢克", "雅列什", "莫伦", "阿泽夫", "萨赫夫", "凯珊"),
        families=("莫伦", "苏维勒", "巴卢克", "雅列什", "凯珊", "哈文", "阿泽夫", "纳沃克", "寇林", "萨赫夫"),
        titles=("导礼师", "观星师", "商队长", "泉庭长", "学塾师", "法官", "医坊师", "香料商", "驿路守", "穹院书记"),
        roots=("扎伊尔", "纳沃克", "哈文", "寇林", "苏维勒", "蕾姆扎", "巴卢克", "雅列什", "莫伦", "阿泽夫", "萨赫夫", "凯珊"),
        region_suffixes=("泉域", "商路", "绿洲辖", "沙海", "月邦", "穹领", "旱谷", "香料区", "星台辖", "蓝穹城邦"),
        place_suffixes=("礼拜院", "商队驿", "泉庭", "穹院", "学塾", "星台", "医坊", "法庭", "香料市", "月塔", "施济院", "书法廊"),
        epithets=("月穹商路民", "蓝穹诸邦", "泉庭礼拜会", "星台学塾民", "香料驿路邦", "绿洲月邦", "几何纹诸城"),
        forbidden=("麦加", "麦地那", "穆罕默德", "阿里", "法蒂玛", "奥马尔", "阿伊莎"),
    ),
}


CATEGORY_ALIASES = {
    "人名": "person",
    "人物": "person",
    "角色": "person",
    "姓名": "person",
    "名字": "person",
    "person": "person",
    "地区": "region",
    "区域": "region",
    "地区名": "region",
    "区域名": "region",
    "国名": "region",
    "州名": "region",
    "region": "region",
    "地点": "place",
    "地点名": "place",
    "地名": "place",
    "节点": "place",
    "node": "place",
    "城市": "place",
    "城镇": "place",
    "聚落": "place",
    "城名": "place",
    "place": "place",
    "别称": "epithet",
    "文明别称": "epithet",
    "称号": "epithet",
    "代称": "epithet",
    "外号": "epithet",
    "epithet": "epithet",
}


GLOBAL_FORBIDDEN = (
    "长安", "洛阳", "北京", "南京", "成都", "杭州", "江南", "中原",
    "奥斯陆", "卑尔根", "雷克雅未克", "乌普萨拉", "奥丁", "索尔", "洛基", "拉格纳",
    "罗马", "凯撒", "奥古斯都", "君士坦丁堡", "庞贝", "亚历山大",
    "伦敦", "约克", "牛津", "剑桥", "威塞克斯", "梅西亚", "亚瑟",
    "利马", "库斯科", "基多", "波哥大", "安第斯", "亚马孙", "印加",
    "耶路撒冷", "伯利恒", "耶稣", "玛利亚", "彼得", "保罗", "约翰",
    "麦加", "麦地那", "穆罕默德", "阿里", "法蒂玛", "奥马尔", "阿伊莎",
)


CULTURE_ALIAS_TO_KEY: dict[str, str] = {}
for key, lex in CULTURES.items():
    for alias in (lex.display, *lex.aliases, key):
        CULTURE_ALIAS_TO_KEY[alias] = key


OVERLAY_KEYS = {"church", "crescent"}
OVERLAY_MARKERS = {
    "church": ("圣钟", "白烛", "银钟", "晨辉", "烛痕", "灰钟"),
    "crescent": ("月穹", "泉庭", "蓝穹", "星台", "香料", "绿洲"),
}
MIXED_EPITHET_TAILS = {
    "church": ("教会", "圣辖", "修院盟", "朝圣会", "钟领"),
    "crescent": ("商路邦", "月邦", "穹领", "泉盟", "驿路"),
}


ALL_NAME_SUFFIXES = tuple(
    sorted(
        {
            suffix
            for lex in CULTURES.values()
            for suffix in (*lex.region_suffixes, *lex.place_suffixes)
        },
        key=len,
        reverse=True,
    )
)
ALL_TITLES = tuple(
    sorted(
        {
            title
            for lex in CULTURES.values()
            for title in lex.titles
        },
        key=len,
        reverse=True,
    )
)


class CultureNameGenerator:
    def __init__(
        self,
        seed: int | str | None = None,
        avoid_similar: bool = True,
        similarity_threshold: float = 0.74,
        history_limit: int = 5000,
        reject_same_core: bool = True,
        max_attempts: int = 8000,
    ):
        self.rng = random.Random(seed)
        self.used: set[str] = set()
        self.used_compact: set[str] = set()
        self.history_by_scope: dict[tuple[tuple[str, ...], str], deque[str]] = {}
        self.avoid_similar = avoid_similar
        self.similarity_threshold = similarity_threshold
        self.history_limit = history_limit
        self.reject_same_core = reject_same_core
        self.max_attempts = max_attempts

    def generate(
        self,
        civilization: str | Sequence[str],
        request: str | None = None,
        count: int = 1,
        unique: bool = True,
    ) -> str | list[str]:
        culture_keys, kind = self._parse(civilization, request)
        scope = (tuple(culture_keys), kind)
        if count < 1:
            return []
        result: list[str] = []
        attempts = 0
        while len(result) < count:
            attempts += 1
            if attempts > self.max_attempts:
                raise RuntimeError("无法生成足够多的不重复或不近似名称。")
            name = self._make(culture_keys, kind)
            if not self._is_safe(name, culture_keys):
                continue
            if unique and self._has_seen_or_similar(name, scope, pending=result):
                continue
            result.append(name)
            if unique:
                self._remember(name, scope)
        return result[0] if count == 1 else result

    def reset_history(self) -> None:
        self.used.clear()
        self.used_compact.clear()
        self.history_by_scope.clear()

    def _parse(self, civilization: str | Sequence[str], request: str | None) -> tuple[list[str], str]:
        if request is None:
            if not isinstance(civilization, str):
                raise ValueError("当 request 为空时，civilization 必须是类似 '中华 人名' 的字符串。")
            kind, culture_text = self._extract_kind_from_command(civilization)
            culture_keys = self._resolve_cultures(culture_text)
            return culture_keys, kind
        kind = self._resolve_kind(request)
        culture_keys = self._resolve_cultures(civilization)
        return culture_keys, kind

    def _extract_kind_from_command(self, command: str) -> tuple[str, str]:
        text = self._clean_text(command)
        for alias in sorted(CATEGORY_ALIASES, key=len, reverse=True):
            if alias in text:
                kind = CATEGORY_ALIASES[alias]
                culture_text = text.replace(alias, " ", 1)
                return kind, culture_text
        raise ValueError(f"无法识别请求类型：{command}。")

    def _resolve_kind(self, request: str) -> str:
        text = self._clean_text(request)
        if text in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[text]
        for alias in sorted(CATEGORY_ALIASES, key=len, reverse=True):
            if alias in text:
                return CATEGORY_ALIASES[alias]
        raise ValueError(f"无法识别请求类型：{request}。")

    def _resolve_cultures(self, civilization: str | Sequence[str]) -> list[str]:
        if isinstance(civilization, str):
            text = self._clean_text(civilization)
            raw_parts = re.split(r"[+＋,，、&\s]+|和", text)
        else:
            raw_parts = [str(x) for x in civilization]
        keys: list[str] = []
        for raw in raw_parts:
            token = self._clean_text(raw)
            if not token or token in {"文明", "文化", "风"}:
                continue
            key = self._resolve_one_culture(token)
            if key not in keys:
                keys.append(key)
        if not keys:
            supported = "、".join(lex.display for lex in CULTURES.values())
            raise ValueError(f"未识别文明。支持：{supported}")
        return keys

    def _resolve_one_culture(self, token: str) -> str:
        variants = [
            token,
            token.replace("文明", ""),
            token.replace("文化", ""),
            token.replace("风", ""),
            token.replace("文明", "").replace("文化", "").replace("风", ""),
        ]
        for variant in variants:
            if variant in CULTURE_ALIAS_TO_KEY:
                return CULTURE_ALIAS_TO_KEY[variant]
        supported = "、".join(lex.display for lex in CULTURES.values())
        raise ValueError(f"未识别文明：{token}。支持：{supported}")

    @staticmethod
    def _clean_text(text: str) -> str:
        return str(text).strip().replace("／", "/").replace("＋", "+").replace("　", " ")

    def _make(self, culture_keys: list[str], kind: str) -> str:
        if len(culture_keys) == 1:
            return self._make_single(culture_keys[0], kind)
        return self._make_mixed(culture_keys, kind)

    def _make_single(self, key: str, kind: str) -> str:
        if kind == "person":
            return self._make_person(key)
        if kind == "region":
            return self._make_region(key)
        if kind == "place":
            return self._make_place(key)
        if kind == "epithet":
            return self._pick(CULTURES[key].epithets)
        raise ValueError(f"未知请求类型：{kind}")

    def _make_mixed(self, keys: list[str], kind: str) -> str:
        base = keys[0]
        overlays = keys[1:]
        overlay = self._pick(overlays)
        if kind == "person":
            if overlay in OVERLAY_KEYS:
                return self._pick(CULTURES[overlay].titles) + self._make_person(base, allow_title=False)
            return self._make_person(self._pick(keys))
        if kind == "region":
            return self._make_mixed_region(base, overlay)
        if kind == "place":
            return self._make_mixed_place(base, overlay)
        if kind == "epithet":
            return self._make_mixed_epithet(base, overlay)
        raise ValueError(f"未知请求类型：{kind}")

    def _make_person(self, key: str, allow_title: bool = True) -> str:
        lex = CULTURES[key]
        if key == "sinic":
            surname = self._pick(lex.surnames)
            given_len = 1 if self.rng.random() < 0.25 else 2
            given = "".join(self._pick_many(lex.given, given_len))
            if self.rng.random() < 0.12:
                given = given + "之"
            name = surname + given
            if allow_title and self.rng.random() < 0.12:
                return self._pick(lex.titles) + name
            return name
        if key == "church":
            return self._pick(lex.titles) + self._pick(lex.given)
        if key == "crescent":
            if self.rng.random() < 0.35:
                return self._pick(lex.titles) + self._pick(lex.given)
            return self._pick(lex.given) + "·" + self._pick(lex.families)
        name = self._pick(lex.given) + "·" + self._pick(lex.families)
        if allow_title and lex.titles and self.rng.random() < 0.14:
            return self._pick(lex.titles) + name
        return name

    def _make_region(self, key: str) -> str:
        lex = CULTURES[key]
        return self._region_root(key) + self._pick(lex.region_suffixes)

    def _make_place(self, key: str) -> str:
        lex = CULTURES[key]
        return self._place_root(key) + self._pick(lex.place_suffixes)

    def _make_mixed_region(self, base: str, overlay: str) -> str:
        base_lex = CULTURES[base]
        overlay_lex = CULTURES[overlay]
        if overlay in OVERLAY_KEYS:
            pattern = self.rng.choice(("base_overlay_suffix", "overlay_base_suffix", "base_marker_suffix"))
            if pattern == "base_overlay_suffix":
                return self._region_root(base) + self._pick(overlay_lex.region_suffixes)
            if pattern == "overlay_base_suffix":
                return self._root_combo(overlay, for_region=True) + self._pick(base_lex.region_suffixes)
            marker = self._pick(OVERLAY_MARKERS[overlay])
            return self._region_root(base) + marker + self._pick(base_lex.region_suffixes)
        return self._region_root(base) + self._pick(overlay_lex.region_suffixes)

    def _make_mixed_place(self, base: str, overlay: str) -> str:
        base_lex = CULTURES[base]
        overlay_lex = CULTURES[overlay]
        if overlay in OVERLAY_KEYS:
            if self.rng.random() < 0.65:
                return self._place_root(base) + self._pick(overlay_lex.place_suffixes)
            return self._root_combo(overlay, for_region=False) + self._pick(base_lex.place_suffixes)
        return self._place_root(base) + self._pick(overlay_lex.place_suffixes)

    def _make_mixed_epithet(self, base: str, overlay: str) -> str:
        if overlay in OVERLAY_KEYS:
            marker = self._pick(OVERLAY_MARKERS[overlay])
            tail = self._pick(MIXED_EPITHET_TAILS[overlay])
            return self._region_root(base) + marker + tail
        return self._region_root(base) + self._region_root(overlay) + self.rng.choice(("盟", "邦", "诸领", "诸州", "城邦"))

    def _region_root(self, key: str) -> str:
        return self._root_combo(key, for_region=True)

    def _place_root(self, key: str) -> str:
        return self._root_combo(key, for_region=False)

    def _root_combo(self, key: str, for_region: bool) -> str:
        lex = CULTURES[key]
        if key == "sinic":
            count = 1 if self.rng.random() < (0.35 if for_region else 0.55) else 2
            return "".join(self._pick_many(lex.roots, count))
        two_root_chance = 0.28 if for_region else 0.20
        count = 2 if self.rng.random() < two_root_chance else 1
        return "".join(self._pick_many(lex.roots, count))

    def _remember(self, name: str, scope: tuple[tuple[str, ...], str]) -> None:
        compact = self._compact(name)
        self.used.add(name)
        self.used_compact.add(compact)
        if scope not in self.history_by_scope:
            self.history_by_scope[scope] = deque(maxlen=self.history_limit)
        self.history_by_scope[scope].append(name)

    def _has_seen_or_similar(
        self,
        name: str,
        scope: tuple[tuple[str, ...], str],
        pending: Sequence[str] = (),
    ) -> bool:
        compact = self._compact(name)
        if name in self.used or compact in self.used_compact:
            return True
        if not self.avoid_similar:
            return False
        old_names = list(self.history_by_scope.get(scope, ())) + list(pending)
        for old in old_names:
            if self._are_names_too_close(name, old):
                return True
        return False

    def _are_names_too_close(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        a_compact = self._compact(a)
        b_compact = self._compact(b)
        if a_compact == b_compact:
            return True
        a_body = self._strip_known_title(a)
        b_body = self._strip_known_title(b)
        if self._compact(a_body) == self._compact(b_body):
            return True
        a_core, a_suffix = self._split_known_suffix(a_body)
        b_core, b_suffix = self._split_known_suffix(b_body)
        if self.reject_same_core:
            if a_core and b_core and a_core == b_core:
                return True
            if a_suffix and b_suffix and a_suffix == b_suffix and self._name_similarity(a_core, b_core) >= 0.82:
                return True
        return self._name_similarity(a_body, b_body) >= self.similarity_threshold

    def _strip_known_title(self, name: str) -> str:
        text = str(name).strip()
        for title in ALL_TITLES:
            if text.startswith(title) and len(text) > len(title):
                return text[len(title):]
        return text

    def _split_known_suffix(self, name: str) -> tuple[str, str]:
        text = self._compact(name)
        for suffix in ALL_NAME_SUFFIXES:
            suffix_compact = self._compact(suffix)
            if text.endswith(suffix_compact) and len(text) > len(suffix_compact):
                return text[:-len(suffix_compact)], suffix_compact
        return text, ""

    def _name_similarity(self, a: str, b: str) -> float:
        a = self._compact(a)
        b = self._compact(b)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        max_len = max(len(a), len(b))
        edit_score = 1.0 - self._levenshtein_distance(a, b) / max_len
        char_score = self._jaccard(set(a), set(b))
        bigram_score = self._jaccard(self._ngrams(a, 2), self._ngrams(b, 2))
        overlap_score = 0.45 * char_score + 0.55 * bigram_score
        return max(edit_score, overlap_score)

    def _is_safe(self, name: str, keys: Iterable[str]) -> bool:
        compact = self._compact(name)
        forbidden = list(GLOBAL_FORBIDDEN)
        for key in keys:
            forbidden.extend(CULTURES[key].forbidden)
        for word in forbidden:
            word_compact = self._compact(word)
            if word_compact and word_compact in compact:
                return False
        return True

    @staticmethod
    def _compact(text: str) -> str:
        return re.sub(r"[·\s\-_/]", "", str(text))

    def _pick(self, seq: Sequence[str]) -> str:
        if not seq:
            raise ValueError("词库为空。")
        return self.rng.choice(tuple(seq))

    def _pick_many(self, seq: Sequence[str], n: int) -> list[str]:
        if n <= 0:
            return []
        seq_tuple = tuple(seq)
        if n >= len(seq_tuple):
            return list(seq_tuple)
        return self.rng.sample(seq_tuple, n)

    @staticmethod
    def _ngrams(text: str, n: int) -> set[str]:
        if len(text) <= n:
            return {text}
        return {text[i:i + n] for i in range(len(text) - n + 1)}

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    @staticmethod
    def _levenshtein_distance(a: str, b: str) -> int:
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
            current = [i]
            for j, cb in enumerate(b, start=1):
                insert_cost = current[j - 1] + 1
                delete_cost = previous[j] + 1
                replace_cost = previous[j - 1] + (0 if ca == cb else 1)
                current.append(min(insert_cost, delete_cost, replace_cost))
            previous = current
        return previous[-1]


def generate(
    civilization: str | Sequence[str],
    request: str | None = None,
    count: int = 1,
    seed: int | str | None = None,
    unique: bool = True,
    avoid_similar: bool = True,
    similarity_threshold: float = 0.74,
):
    gen = CultureNameGenerator(
        seed=seed,
        avoid_similar=avoid_similar,
        similarity_threshold=similarity_threshold,
    )
    return gen.generate(
        civilization=civilization,
        request=request,
        count=count,
        unique=unique,
    )
