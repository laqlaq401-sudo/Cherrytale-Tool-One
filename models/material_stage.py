"""素材关卡与元素试炼（MaterialStage）元数据、层级子目录与协议模型。

【关卡体系】
素材关卡属于日常固定开放关卡，按类别分为：
1. **角色素材（job）**：7 个区域（101~107），涵盖战士/守卫/强袭/魔导/灵疗/狙杀/好感礼物；
2. **元素试炼（element）**：6 个区域（201~206），涵盖力量/知识/敏捷/技巧/意志/生命试炼。

【云端校验与安全性】
依据 DataRelative 与 SpecialSectionData 配置表分析：
- 13 个区域共 64 个关卡在服务端均配置为 cheatCheck == 0、energyCost == 0；
- **扫荡（11009/11010）**：直接向服务端提交 sectionID 与次数，零战斗逻辑校验；
- **首通（11015/11017）**：服务端不校验战斗逻辑与存证（cheatCheck==0），仅做时间戳签名校验。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
import time
from functools import lru_cache
from typing import Any, Final

from pydantic import Field

from models.config_table import TABLE_ENCODING
from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage
from models.config_table import read_table, split_line, to_int, TableError
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("models.material_stage")

#: 业务成功码
MATERIAL_STAGE_SUCCESS: Final[int] = SUCCESS_ERROR_CODE

#: 客户端首通签名静态盐值（与降临关/普通主线关卡保持同构）
FIGHT_SIGN_SALT: Final[str] = "03120908"

#: 允许出站的消息白名单（区域清单、扫荡、首通握手、首通结算）
#:
#: ★ 2026-10-03 修订：移除 ``11003 GetSectionByAreaPacket`` —— 10.3 素材抓包
#: 证明真实客户端的素材流程是 ``11001（拉区域清单）→ 11009（直接扫）``，
#: **不发 11003**；实测对素材区发 11003 服务端只回**空子包**
#: （它只适用于降临/活动区，见 tasks/sweep.py）。新增 ``11001``。
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GetAllItemPacket",          # 2007 只读拉背包（数扫荡券）
    "GetAreaByDifficultyPacket", # 11001 按难度拉区域状态清单（素材入口，difficultyType=-3）
    "SweepSectionPacket",        # 11009 关卡扫荡
    "FightingRequestPacket",     # 11015 创建战斗 Token（首通握手）
    "FightingResultPacket",      # 11017 战斗结束结算（首通发奖）
)

ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: 明确禁止出站的报文（绝不自动买次数、绝不买体力）
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "BuyActivityStagePacket",  # 11027 购买关卡挑战次数
    "BuyEnergyPacket",         # 24005 购买体力
)

FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    2007: "拉取背包全量列表（只读；只用来数扫荡券够不够扫）",
    11001: "拉取素材/元素关卡的区域状态清单（只读；校验目标区域是否开放）",
    11009: "素材/元素关卡零体力扫荡（零消耗/消耗每日免费次数）",
    11015: "简单关卡安全首通握手（cheatCheck=0，无战斗复算）",
    11017: "简单关卡安全首通结算（cheatCheck=0，零消耗）",
}


@dataclass(frozen=True)
class MaterialSection:
    """单个素材/元素试炼关卡。"""

    section_id: int
    display_id: int
    name: str
    difficulty: int
    difficulty_name: str
    level_limit: int = 1
    cheat_check: int = 0
    energy_cost: int = 0
    #: 扫荡开关（``SpecialSectionData.sweepOff``；非 0 表示不可扫荡）
    sweep_off: int = 0
    #: 每日次数上限（``SpecialSectionData.limit``；0 = 无独立上限）
    daily_limit: int = 0

    def as_view(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "display_id": self.display_id,
            "name": self.name,
            "difficulty": self.difficulty,
            "difficulty_name": self.difficulty_name,
            "level_limit": self.level_limit,
            "cheat_check": self.cheat_check,
            "energy_cost": self.energy_cost,
            "sweep_off": self.sweep_off,
            "daily_limit": self.daily_limit,
        }


@dataclass(frozen=True)
class MaterialArea:
    """素材关卡/元素试炼的区域分类（子目录）。"""

    area_id: int
    display_id: int
    name: str
    category: str  # "job" 或 "element"
    category_name: str  # "角色素材" 或 "元素试炼"
    limit_per_day: int
    sections: tuple[MaterialSection, ...]

    def as_view(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "display_id": self.display_id,
            "name": self.name,
            "category": self.category,
            "category_name": self.category_name,
            "limit_per_day": self.limit_per_day,
            "sections": [s.as_view() for s in self.sections],
        }

    def get_highest_section(self) -> MaterialSection:
        """获取本区域难度最高（关卡号最大）的关卡。"""
        return max(self.sections, key=lambda s: s.difficulty)


# ---------------------------------------------------------------------------
# 目录构建：从配置表读**真实**区域/关卡 ID（2026-10-04 整体重写）
# ---------------------------------------------------------------------------
#: 【为什么必须重建】旧目录的 area/section ID 是本地**编造**的
#: （128000070 / 10104 这类号码服务端根本不认）。真实数据链（全部来自本地
#: TextAsset，且与 10.3 抓包的服务端 11002 清单实测吻合）：
#:
#: 1. ``DataRelative`` 的 ``MaterialAreaData|specialAreaID`` 值行
#:    → 13 个**真实 areaID**（顺序 = MaterialAreaData 行序：
#:    前 7 行 regionType=1 职业素材、后 6 行 regionType=2 元素试炼）：
#:    ``128000020~026``（職業挑戰）与 ``128010001~006``（元素試煉塔）；
#: 2. ``SpecialAreaData.areaName`` → 区域官方名（鎮守挑戰 / 火之元素試煉塔 …）；
#: 3. ``DataRelative`` 的 ``SpecialSectionData|areaID`` 值行 → 每个关卡归属的区域；
#: 4. ``SpecialSectionData`` → 关卡本体（sectionID 形如 129010056 ——
#:    10.3 抓包里真实扫荡提交的就是它）。
_JOB_CATEGORY: Final[str] = "job"
_ELEMENT_CATEGORY: Final[str] = "element"


def _datarelative_values(key: str) -> list[str | None]:
    """读 ``DataRelative`` 里指定关系键的**值行**（键行的下一行）。

    ``DataRelative`` 的格式是"键行（表|列|表|列）+ 值行（竖线分隔）"，
    与 ``models.config_table`` 的对齐表格不同，所以这里单独解析。
    """
    from models.game_config import table_path

    path = table_path("DataRelative")
    lines = path.read_text(encoding=TABLE_ENCODING).splitlines()
    key_parts = key.split("|")
    for index, line in enumerate(lines):
        # 键行是四段式"表|列|表|列"，只按前两段（表|列）匹配
        if line.split("|")[:2] == key_parts and index + 1 < len(lines):
            return split_line(lines[index + 1])
    raise TableError(path, 0, f"DataRelative 里找不到关系行 {key!r}")


@lru_cache(maxsize=1)
def _load_material_catalog() -> tuple[MaterialArea, ...]:
    """从配置表构建 13 个素材区目录（真 ID、真名称、真关卡）。

    :raises TableError / FileNotFoundError: 配置表缺失或关系行缺失时
        （素材目录没有"编造兜底"—— ID 错了发包就是白发，宁可启动即报错）。
    """
    from models.game_config import table_path

    area_rows = read_table(table_path("MaterialAreaData"))
    area_ids = _datarelative_values("MaterialAreaData|specialAreaID")
    if len(area_rows) != len(area_ids):
        raise TableError(
            table_path("MaterialAreaData"), 0,
            f"MaterialAreaData 行数({len(area_rows)})与 DataRelative 的"
            f" specialAreaID 值数({len(area_ids)})不一致 —— 表结构可能更新过",
        )
    special_rows = {row.get("areaID"): row for row in read_table(table_path("SpecialAreaData"))}
    section_rows = read_table(table_path("SpecialSectionData"))
    section_area_ids = _datarelative_values("SpecialSectionData|areaID")

    sections_by_area: dict[int, list[dict[str, str | None]]] = {}
    for row, area_text in zip(section_rows, section_area_ids):
        if area_text is None:
            continue
        sections_by_area.setdefault(int(area_text), []).append(row)

    areas: list[MaterialArea] = []
    for area_row, area_text in zip(area_rows, area_ids):
        if area_text is None:
            raise TableError(
                table_path("MaterialAreaData"), 0,
                "specialAreaID 值行里有空槽 —— 与服务端 13 区清单不符",
            )
        area_id = int(area_text)
        category = (
            _JOB_CATEGORY if area_row.get("regionType") == "1" else _ELEMENT_CATEGORY
        )
        special = special_rows.get(area_text, {})
        area_name = special.get("areaName") or str(area_row.get("regionName") or area_text)

        sections: list[MaterialSection] = []
        for order, section_row in enumerate(sections_by_area.get(area_id, []), start=1):
            sections.append(
                MaterialSection(
                    section_id=int(section_row["sectionID"]),
                    display_id=order,
                    name=section_row.get("sectionName") or f"關卡{order}",
                    difficulty=order,
                    difficulty_name=f"第 {order} 關",
                    level_limit=max(to_int(section_row, "LVRequire") or 1, 1),
                    cheat_check=to_int(section_row, "cheatCheck") or 0,
                    energy_cost=to_int(section_row, "energyCost") or 0,
                    sweep_off=to_int(section_row, "sweepOff") or 0,
                    daily_limit=to_int(section_row, "limit") or 0,
                )
            )
        if not sections:
            raise TableError(
                table_path("SpecialSectionData"), 0,
                f"区域 {area_id}（{area_name}）没有任何关卡 —— DataRelative 关系行缺失",
            )

        areas.append(
            MaterialArea(
                area_id=area_id,
                display_id=0,  # 下面统一编号
                name=area_name,
                category=category,
                category_name=str(area_row.get("regionName") or ""),
                limit_per_day=0,
                sections=tuple(sections),
            )
        )

    # display_id：职业素材 101~107、元素试炼 201~206（与旧版编号习惯一致，
    # 用户脚本/界面里按 101、201 选区的用法不受影响）
    job_index = 0
    element_index = 0
    numbered: list[MaterialArea] = []
    for area in areas:
        if area.category == _JOB_CATEGORY:
            job_index += 1
            numbered.append(replace(area, display_id=100 + job_index))
        else:
            element_index += 1
            numbered.append(replace(area, display_id=200 + element_index))
    return tuple(numbered)


def get_material_areas(category: str = "all") -> tuple[MaterialArea, ...]:
    """根据大类筛选素材关卡区域目录。

    :param category: ``"all"`` 全部、``"job"`` 角色素材、``"element"`` 元素试炼。
    """
    cat = (category or "all").strip().lower()
    catalog = _load_material_catalog()
    if cat in ("all", "*", ""):
        return catalog
    return tuple(a for a in catalog if a.category == cat)


def resolve_material_area(
    query: str | int,
    category: str = "all",
) -> MaterialArea | None:
    """根据用户输入的名称关键字、display_id 或 area_id 定位素材区域。"""
    if query is None or query == "":
        return None

    areas = get_material_areas(category)
    q_str = str(query).strip().lower()

    if q_str.isdigit():
        q_int = int(q_str)
        # 1. 尝试按 display_id (如 101, 201) 精确匹配
        for a in areas:
            if a.display_id == q_int:
                return a
        # 2. 尝试按 area_id (如 128000010) 精确匹配
        for a in areas:
            if a.area_id == q_int:
                return a

    # 3. 按名称或别名包含匹配
    for a in areas:
        if q_str in a.name.lower() or q_str in a.category_name.lower():
            return a

    return None


def resolve_material_section(
    query: str | int,
    area: MaterialArea | None = None,
) -> tuple[MaterialArea, MaterialSection] | None:
    """根据用户输入定位指定关卡。"""
    if query is None or query == "":
        return None

    target_areas = (area,) if area is not None else _load_material_catalog()
    q_str = str(query).strip().lower()

    if q_str.isdigit():
        q_int = int(q_str)
        for a in target_areas:
            for s in a.sections:
                if s.section_id == q_int or s.display_id == q_int:
                    return a, s

    for a in target_areas:
        for s in a.sections:
            if q_str in s.name.lower():
                return a, s

    return None


# ---------------------------------------------------------------------------
# 协议模型（直接复用并重导出通用关卡与战斗报文）
# ---------------------------------------------------------------------------
class GetAreaByDifficultyPacket(ProtoMessage):
    """按难度拉取区域状态清单（消息号 11001）。

    【★ 10.3 素材抓包】素材/元素关的入口查询是 ``difficultyType = -3``；
    真实客户端拉到清单后**直接扫**（11009），并不发 11003 ——
    11003 只适用于降临/活动区，对素材区发它服务端只回空子包。
    """

    proto_class_name = "GetAreaByDifficultyPacket"

    #: 难度类型（素材/元素关实测 = -3）
    difficultyType: int = 0
    #: 区域 ID（素材流程不填）
    regionID: int = 0


class AreaStateClass(ProtoMessage):
    """一个区域的状态（``GetAreaByDifficultyRes.areaStateList`` 的元素）。

    字段定义来自 dump.cs ``AreaStateClass``（TypeDefIndex 9041），
    与 10.3 抓包逐字段核对一致（17 B 元素）。
    """

    proto_class_name = "AreaStateClass"

    #: 区域 ID
    areaID: int = 0
    #: 区域状态
    areaState: int = 0
    #: 区域星级（= 该区关卡总数，抓包里素材区为 12 / 18）
    areaStars: int = 0
    #: 已领的星级宝箱（repeated）
    receiveStarReward: list[int] = Field(default_factory=list)
    #: 通关次数
    clearCount: int = 0


class GetAreaByDifficultyRes(ProtoMessage):
    """区域状态清单响应（消息号 11002）。"""

    proto_class_name = "GetAreaByDifficultyRes"

    #: 区域状态清单
    areaStateList: list[AreaStateClass] = Field(default_factory=list)
    #: 剧情关 ID 清单（素材流程用不到）
    hStoryIdList: list[int] = Field(default_factory=list)
    #: 辉煌航迹奖励次数（挂在区域响应上的跨系统字段）
    grandLineRewardTimes: int = 0


from models.activity_stage import (
    SWEEP_TICKET_ITEM_ID,
    GetSectionByAreaPacket,
    GetSectionByAreaRes,
    SectionStateClass,
    SweepSectionPacket,
    SweepSectionRes,
    normalize_sweep_times,
)
from models.main_stage import (
    FightingRequestPacket,
    FightingResponseRes,
    FightingResultPacket,
    FightingResultRes,
    FightDataClass,
    make_battle_server_verify,
)


def make_fight_sign(fight_token: str, time_cost_sec: int, win_lose: int = 1) -> str:
    """计算战斗结果签名：MD5(token + timeCost + winLose + "03120908")。"""
    raw = f"{fight_token}{time_cost_sec}{win_lose}{FIGHT_SIGN_SALT}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
