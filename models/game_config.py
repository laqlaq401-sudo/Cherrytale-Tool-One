"""日常任务相关的配置表访问层（把「列名」翻译成「业务字段」）。

【为什么把通用解析与业务表分成两个文件】
``config_table.py`` 只负责「把文本变成字典」，不含任何游戏知识；
本文件负责「这些列分别是什么意思、哪张表对应哪个业务概念」。分开的好处：

- 解析器可以被成就表、商店表、邮件表等**所有**配置表复用；
- 业务语义变化（游戏更新加列、改列名）只需要改本文件，不会波及解析逻辑。

【本文件覆盖的表（列数与行数均已实测）】

=====================================  =========  ====  ==================================
配置表                                  行数        列数  用途
=====================================  =========  ====  ==================================
``DailyMissionData``                    46         18    日常任务本体
``DailyMissionRewardData``              5          4     活跃度宝箱
``ActiveMissionData``                   7515       20    活跃任务（长线）
=====================================  =========  ====  ==================================

【实测提醒：一个很容易搞错的 ID】
``DailyMissionRewardData`` 的 ``dmrID`` 是 **939000001~939000005**，
并不是 ``requiredNum`` 的 1/3/5/7/9。``requiredNum`` 的含义是「需要多少活跃点」。

因此领活跃宝箱（消息号 6003 ``DMActReward``）时要发的参数是 **dmrID**；
而「当前活跃点够开哪个箱子」要拿 ``requiredNum`` 去比。两者用反了会一直报参数错误。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import config
from models.config_table import (
    TABLE_ENCODING,
    TableError,
    TableRow,
    read_table,
    to_int,
    to_int_list,
    to_text,
)

#: 日常任务配置表文件名（TextAsset 目录下无扩展名）
TABLE_DAILY_MISSION: Final[str] = "DailyMissionData"

#: 活跃度宝箱配置表文件名
TABLE_DAILY_MISSION_REWARD: Final[str] = "DailyMissionRewardData"

#: 活跃任务配置表文件名（长线任务，日常流程里只用于展示）
TABLE_ACTIVE_MISSION: Final[str] = "ActiveMissionData"


def table_path(name: str) -> Path:
    """拼出配置表的绝对路径。

    :raises FileNotFoundError: 文件不存在时，提示里会带上可用的环境变量，
        避免你只看到一句「No such file」而不知道素材目录是可以搬家的。
    """
    path = config.TEXT_ASSET_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到配置表 {name}：{path}\n"
            "  若素材放在别处，请设置环境变量 CHERRYTALE_MATERIAL_ROOT 指向它"
        )
    return path


@dataclass(frozen=True)
class DailyMission:
    """一条日常任务配置（``DailyMissionData`` 的一行）。

    字段名已从「配置表列名」翻译成自解释的形式；需要原始列值时用 :attr:`raw`，
    这样将来要用到新列时不必回来改这个类。
    """

    #: ``dmID`` —— 任务 ID（领奖时作为 ``DMReward.misID`` 发给服务端）
    mission_id: int
    #: ``misTitle`` / ``misDescription`` —— 标题与描述（描述里的 ``{0}`` 会被替换成次数）
    title: str | None
    description: str | None
    #: ``actionID`` —— 行为类型编码（字符串，如 ``"070101"``）。
    #: ⚠️ 它是字符串而不是枚举，含义必须结合 ``go_to``（跳到哪个系统）判断。
    action_id: str | None
    #: ``actionTarget`` —— 行为目标参数（如 ``"id:-1"``），部分任务为 ``NULL``
    action_target: str | None
    #: ``count`` —— 需要完成的次数
    required_count: int
    #: ``addPoint`` —— 完成后增加的活跃点
    add_point: int
    #: ``rewardTipItem`` —— 奖励展示用的道具 ID
    reward_tip_item: int | None
    #: ``itemAmount``
    item_amount: int
    #: ``missionVIPRequie`` —— VIP 要求（0 表示不要求）
    vip_require: int
    #: ``goTo`` —— 该任务对应的系统 ID（点「前往」会跳到哪个界面）
    go_to: int | None
    #: ``goToParameter`` —— 跳转参数
    go_to_parameter: tuple[int, ...]
    #: ``status``
    status: int
    #: ``misSubSorting`` —— 同组内的排序
    sort: int
    #: 原始行（保底手段：将来需要新列时不必改本类）
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号（报错定位用）。"""
        return self.raw.line_no


@dataclass(frozen=True)
class DailyMissionRewardBox:
    """一个活跃度宝箱（``DailyMissionRewardData`` 的一行）。"""

    #: ``dmrID`` —— 宝箱 ID，**领奖时作为 ``DMActReward.actRewardID`` 发出**
    box_id: int
    #: ``requiredNum`` —— 开启该宝箱所需的活跃点
    required_point: int
    #: ``boxImgType`` —— 图标类型（纯展示用途）
    box_img_type: int
    #: 原始行
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号（报错定位用）。"""
        return self.raw.line_no


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def _int_or(row: TableRow, key: str, fallback: int) -> int:
    """取整数字段，为空（``NULL``）时用 ``fallback``。

    【为什么不用 ``to_int(row, key, default=fallback) or fallback`` 这种写法】
    ``0`` 在布尔语境里是假值，``... or fallback`` 会把**真实的 0 当成缺失**替换掉。
    配置表里 ``addPoint``、``itemAmount`` 等列的合法值本来就包含 0，
    这类 bug 会让「0 活跃点的任务」变成「fallback 活跃点」，且完全不报错。
    """
    value = to_int(row, key)
    return fallback if value is None else value


def load_daily_missions(path: Path | None = None) -> list[DailyMission]:
    """读取全部日常任务配置（``DailyMissionData``，实测 46 条）。

    :param path: 配置表路径；``None`` 表示自动定位到素材目录下的同名文件。
    :raises TableError: 某行缺少主键 ``dmID`` 时（带文件名与行号）。
    """
    rows = read_table(path or table_path(TABLE_DAILY_MISSION))
    missions: list[DailyMission] = []
    for row in rows:
        mission_id = to_int(row, "dmID")
        if mission_id is None:
            # dmID 是主键，缺了它这条记录无法使用。
            # 这里刻意抛错而不是跳过：静默跳过会让「任务少了一条」完全无从察觉。
            raise TableError(row.path, row.line_no, "缺少主键 dmID")
        missions.append(
            DailyMission(
                mission_id=mission_id,
                title=to_text(row, "misTitle"),
                description=to_text(row, "misDescription"),
                action_id=to_text(row, "actionID"),
                action_target=to_text(row, "actionTarget"),
                required_count=_int_or(row, "count", 1),
                add_point=_int_or(row, "addPoint", 0),
                reward_tip_item=to_int(row, "rewardTipItem"),
                item_amount=_int_or(row, "itemAmount", 0),
                vip_require=_int_or(row, "missionVIPRequie", 0),
                go_to=to_int(row, "goTo"),
                go_to_parameter=tuple(to_int_list(row, "goToParameter")),
                status=_int_or(row, "status", 0),
                sort=_int_or(row, "misSubSorting", 0),
                raw=row,
            )
        )
    return missions


def load_daily_reward_boxes(path: Path | None = None) -> list[DailyMissionRewardBox]:
    """读取活跃度宝箱配置（``DailyMissionRewardData``，实测 5 条）。

    :raises TableError: 某行缺少主键 ``dmrID`` 时。
    """
    rows = read_table(path or table_path(TABLE_DAILY_MISSION_REWARD))
    boxes: list[DailyMissionRewardBox] = []
    for row in rows:
        box_id = to_int(row, "dmrID")
        if box_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 dmrID")
        boxes.append(
            DailyMissionRewardBox(
                box_id=box_id,
                required_point=_int_or(row, "requiredNum", 0),
                box_img_type=_int_or(row, "boxImgType", 0),
                raw=row,
            )
        )
    return boxes


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def mission_index(missions: list[DailyMission]) -> dict[int, DailyMission]:
    """建立 ``任务 ID → 配置`` 的索引（服务端只回 ID，靠它还原出标题与所需次数）。"""
    return {mission.mission_id: mission for mission in missions}


def boxes_for_point(
    boxes: list[DailyMissionRewardBox], point: int
) -> list[DailyMissionRewardBox]:
    """筛出「当前活跃点已经够开」的宝箱，按所需活跃点升序返回。

    注意：本函数只看**活跃点**，不看服务端记录的「是否已领」。
    是否已领必须由服务端的 ``dmrReceivedList`` 决定 ——
    本地数据永远可能过期，用它来跳过领取会造成漏领。
    """
    return sorted(
        (box for box in boxes if box.required_point <= point),
        key=lambda box: box.required_point,
    )


def action_id_summary(missions: list[DailyMission]) -> dict[str, int]:
    """统计 ``actionID`` 的分布：``{行为编码: 出现次数}``。

    用途：决定「哪些日常任务可以靠发包完成、哪些必须人工」时，
    先看这份分布比逐个任务翻配置表快得多。
    """
    summary: dict[str, int] = {}
    for mission in missions:
        key = mission.action_id or "<未设置>"
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items(), key=lambda item: (-item[1], item[0])))


# ---------------------------------------------------------------------------
# 周常任务与周常活跃宝箱
# ---------------------------------------------------------------------------
#: 周常任务配置表文件名
TABLE_WEEKLY_MISSION: Final[str] = "WeeklyMissionData"

#: 周常活跃宝箱配置表文件名
TABLE_WEEKLY_MISSION_REWARD: Final[str] = "WeeklyMissionRewardData"


@dataclass(frozen=True)
class WeeklyMission:
    """一条周常任务配置（``WeeklyMissionData`` 的一行）。

    下面刻意沿用 :class:`DailyMission` 的字段命名（``mission_id`` /
    ``required_count`` / ``add_point``），这样"算活跃点"的函数
    （见 :func:`total_active_point`）能同时吃日常与周常两种配置，
    不必写两套几乎相同的代码。

    【实测列名与含义】
    ``wmID|misTitle|misDescription|misSubSorting|nextMisSub|actionID|actionTarget|
    count|addPoint|reward|rewardTipItem|missionVIPRequie|activeMissionType|
    goTo|goToParameter|status``

    第一条实测数据是 ``250000001|冒險展開！|累積登入{0}天|…|5|1|…`` ——
    即"累计登录 5 天，给 1 点周常活跃"。
    """

    #: ``wmID`` —— 任务 ID（领奖时报给服务端的仍是 ``DMReward.misID``）
    mission_id: int
    #: 标题 / 描述（描述里的 ``{0}`` 会被替换成次数）
    title: str | None
    description: str | None
    #: ``actionID`` 行为编码（字符串）
    action_id: str | None
    #: ``actionTarget`` 行为目标（如 ``"id:200000002"`` = 消耗体力系任务）
    action_target: str | None
    #: ``count`` 需要完成的次数
    required_count: int
    #: ``addPoint`` 完成后增加的**周常活跃点**
    add_point: int
    #: ``rewardTipItem`` 奖励展示用道具
    reward_tip_item: int | None
    #: ``missionVIPRequie`` VIP 要求（0 = 不限）
    vip_require: int
    #: ``misSubSorting`` 同组内排序
    sort: int
    #: 原始行（保底手段：将来需要新列时不必改本类）
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号（报错定位用）。"""
        return self.raw.line_no


@dataclass(frozen=True)
class WeeklyMissionRewardBox:
    """一个周常活跃度宝箱（``WeeklyMissionRewardData`` 的一行）。

    【实测数据】``wmrID|requiredNum|boxImgType|reward``::

        251000000|1|0|NULL
        251000001|3|1|NULL
        251000002|5|2|NULL
        251000003|7|3|NULL
        251000004|9|4|NULL

    与日常宝箱同为 4 列，但 **ID 段完全不同**（日常 939000001~5、周常 251000000~4）。
    领周常宝箱时 ``DMReward.misID`` 传的是 ``wmrID``、``mistypeID`` 传 15
    （``MissionType.WEEKLY_REWARD_BOX``）。
    """

    #: ``wmrID`` —— 宝箱 ID（领奖时作为 ``DMReward.misID``）
    box_id: int
    #: ``requiredNum`` —— 需要多少**周常活跃点**
    required_point: int
    #: ``boxImgType`` 图片样式
    box_img_type: int
    #: 原始行
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号。"""
        return self.raw.line_no


def load_weekly_missions(path: Path | None = None) -> list[WeeklyMission]:
    """读取全部周常任务配置（``WeeklyMissionData``）。

    :param path: 配置表路径；``None`` 表示自动定位到素材目录下的同名文件。
    :raises TableError: 某行缺少主键 ``wmID`` 时（带文件名与行号）。
    """
    rows = read_table(path or table_path(TABLE_WEEKLY_MISSION))
    missions: list[WeeklyMission] = []
    for row in rows:
        mission_id = to_int(row, "wmID")
        if mission_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 wmID")
        missions.append(
            WeeklyMission(
                mission_id=mission_id,
                title=to_text(row, "misTitle"),
                description=to_text(row, "misDescription"),
                action_id=to_text(row, "actionID"),
                action_target=to_text(row, "actionTarget"),
                required_count=_int_or(row, "count", 1),
                add_point=_int_or(row, "addPoint", 0),
                reward_tip_item=to_int(row, "rewardTipItem"),
                vip_require=_int_or(row, "missionVIPRequie", 0),
                sort=_int_or(row, "misSubSorting", 0),
                raw=row,
            )
        )
    return missions


def load_weekly_reward_boxes(
    path: Path | None = None,
) -> list[WeeklyMissionRewardBox]:
    """读取周常活跃宝箱配置（``WeeklyMissionRewardData``，实测 4 条）。

    :raises TableError: 某行缺少主键 ``wmrID`` 时。
    """
    rows = read_table(path or table_path(TABLE_WEEKLY_MISSION_REWARD))
    boxes: list[WeeklyMissionRewardBox] = []
    for row in rows:
        box_id = to_int(row, "wmrID")
        if box_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 wmrID")
        boxes.append(
            WeeklyMissionRewardBox(
                box_id=box_id,
                required_point=_int_or(row, "requiredNum", 0),
                box_img_type=_int_or(row, "boxImgType", 0),
                raw=row,
            )
        )
    return boxes


# ---------------------------------------------------------------------------
# 活跃点计算（日常与周常共用一套逻辑）
# ---------------------------------------------------------------------------
def total_active_point(
    missions: Sequence[DailyMission] | Sequence[WeeklyMission],
    completed_times: Mapping[int, int],
) -> int:
    """按**服务端返回的完成次数**累加活跃点。

    :param missions: 任务配置（日常用 :func:`load_daily_missions`、
        周常用 :func:`load_weekly_missions`）。两者字段名一致，所以能共用本函数。
    :param completed_times: ``{任务 ID: 已完成次数}``，来自服务端响应
        （``DailyMissionListClass.completedTimes``），**不要用本地记录推算**。
    :return: 活跃点合计。

    【为什么必须用服务端的 completedTimes】
    玩家可能在手机端、网页端都做过任务，本地推算的"我做了几次"永远可能落后。
    用落后数据算出的活跃点偏低，结果是"以为还不够、其实早能领了" ——
    表现为**静默漏领**，你很难察觉。

    【为什么是"够次数才加点"而不是"按次数比例加"】
    配置里的 ``addPoint`` 是**整条任务完成**后的奖励，不是每做一次给一点：
    例如"累计登录 5 天给 1 点"，做到第 3 天时并不给分。
    """
    total = 0
    for mission in missions:
        done = completed_times.get(mission.mission_id, 0)
        if done >= mission.required_count:
            total += mission.add_point
    return total


def boxes_reachable(
    boxes: Sequence[DailyMissionRewardBox] | Sequence[WeeklyMissionRewardBox],
    point: int,
) -> list[int]:
    """筛出"活跃点已经够"的宝箱 ID（按所需点数升序）。

    :param boxes: 宝箱配置（日常或周常任一种，二者字段名一致）。
    :param point: 当前活跃点（用 :func:`total_active_point` 算出来）。
    :return: 宝箱 ID 列表。

    .. note::
       与 :func:`boxes_for_point` 的分工：那个返回完整的宝箱对象（日常流程用），
       本函数只返回 ID（日常/周常两条链路共用同一套日志格式）。

    .. warning::
       本函数**不知道服务端状态** —— 是否已经领过必须看
       ``DailyMissionRes.dmrReceivedList`` / ``wmrReceivedList``。
       拿本地配置去决定"不领了"是漏领的常见成因。
    """
    return [
        box.box_id
        for box in sorted(boxes, key=lambda item: item.required_point)
        if box.required_point <= point
    ]


# ---------------------------------------------------------------------------
# 宴席档位（ExtraEnergyData）—— "会不会花钻石"的判定依据
# ---------------------------------------------------------------------------
#: 宴席档位配置表文件名
TABLE_EXTRA_ENERGY: Final[str] = "ExtraEnergyData"


@dataclass(frozen=True)
class ExtraEnergyTier:
    """一个宴席档位（``ExtraEnergyData`` 的一行）。

    【实测数据】``menuID|number|assetID|selfReward|friendReward|useItemID|itemAmount``::

        500000001|1|Activity_Eat_food1|NULL|NULL|NULL|0      ← 免费档
        500000002|2|Activity_Eat_food2|NULL|NULL|NULL|7
        500000003|3|Activity_Eat_food3|NULL|NULL|NULL|15
        500000004|4|Activity_Eat_food4|NULL|NULL|NULL|24
        500000005|5|Activity_Eat_food5|NULL|NULL|NULL|31

    ``useItemID`` 为 ``NULL`` 表示消耗的是**默认货币（钻石）**，此时 ``itemAmount``
    就是钻石数；第 1 档为 0，即"免费请客"。这正是"可以选择是否花钻石邀请好友"
    的数据来源 —— 也是为什么宴席任务必须过消费闸门。

    【★ 2026-10-04 实测定案：``number`` = 参与人数（**含自己**），不是好友数】
    依据：免费吃（不带好友、0 钻）对应 ``number=1``；10.3 抓包的付费邀请带
    恰好 4 位好友、对应最大档 ``number=5``（31 钻）。所以：
    ``好友数 = number - 1``（见 :attr:`friend_count`），上限 4 位
    （``config.BBQ_MAX_INVITE_FRIENDS``）；2026-10-04 发 5 位好友被服务端
    拒 ``errorCode=-6``。

    .. note::
       ``selfReward`` / ``friendReward`` 是 ``OtherRewardData`` 结构（本次未建模），
       读取时跳过。将来要展示"请客后双方各得什么"时再补这两个字段。
    """

    #: ``menuID`` 档位 ID
    menu_id: int
    #: ``number`` 第几档（1 起）
    number: int
    #: ``assetID`` 表现资源名（如 ``Activity_Eat_food1``）
    asset_id: str | None
    #: ``useItemID`` 消耗的道具 ID；``None`` 表示消耗**默认货币（钻石）**
    use_item_id: int | None
    #: ``itemAmount`` 消耗数量（``use_item_id`` 为 ``None`` 时即钻石数）
    item_amount: int
    #: 原始行（保底手段）
    raw: TableRow = field(compare=False, repr=False)

    @property
    def friend_count(self) -> int:
        """该档位对应的好友数（``number - 1``：``number`` 含自己，见类文档）。"""
        return max(self.number - 1, 0)

    @property
    def is_free(self) -> bool:
        """是否免费档（什么都不消耗）。"""
        return self.item_amount <= 0

    @property
    def spends_diamond(self) -> bool:
        """是否消耗**钻石**（即"花默认货币"）。

        :return: 非免费、且 ``useItemID`` 为空时返回 ``True``。
        """
        return not self.is_free and self.use_item_id is None

    def describe(self) -> str:
        """一行摘要（日志与演练输出用）。"""
        if self.is_free:
            return f"档位 {self.menu_id}（第 {self.number} 档）：免费"
        if self.spends_diamond:
            return f"档位 {self.menu_id}（第 {self.number} 档）：{self.item_amount} 钻石"
        return (
            f"档位 {self.menu_id}（第 {self.number} 档）："
            f"道具 {self.use_item_id}×{self.item_amount}"
        )

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号。"""
        return self.raw.line_no


def load_extra_energy_tiers(path: Path | None = None) -> list[ExtraEnergyTier]:
    """读取宴席档位配置（``ExtraEnergyData``，实测 5 条）。

    :raises TableError: 某行缺少主键 ``menuID`` 时。
    """
    rows = read_table(path or table_path(TABLE_EXTRA_ENERGY))
    tiers: list[ExtraEnergyTier] = []
    for row in rows:
        menu_id = to_int(row, "menuID")
        if menu_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 menuID")
        tiers.append(
            ExtraEnergyTier(
                menu_id=menu_id,
                number=_int_or(row, "number", 0),
                asset_id=to_text(row, "assetID"),
                use_item_id=to_int(row, "useItemID"),
                item_amount=_int_or(row, "itemAmount", 0),
                raw=row,
            )
        )
    return tiers


# ---------------------------------------------------------------------------
# 工会个人矿点（AlliancePersonalPitData）
#   —— 一键开矿（21047）填 ``roleSidList`` 的**人数依据**（10.3 抓包实证）
# ---------------------------------------------------------------------------
#: 个人矿点模板表（``Cherrytale Asset/TextAsset/AlliancePersonalPitData``）
TABLE_PERSONAL_PIT: Final[str] = "AlliancePersonalPitData"


#: 条件类型（与客户端 ``ePitLimitType`` 一一对应，顺序也一致）
PIT_CONDITION_KINDS: Final[tuple[str, ...]] = (
    "qualification",
    "element",
    "job",
    "star",
    "lv",
    "quality",
    "race",
)

#: 条件类型 → 可读名（日志/报缺用）
PIT_CONDITION_LABELS: Final[dict[str, str]] = {
    "qualification": "资质",
    "element": "元素",
    "job": "职业",
    "star": "星级",
    "lv": "等级",
    "quality": "品质",
    "race": "种族",
}

#: 条件类型 → ``(从哪取值, 比较方式)``。
#:
#: ★ **依据是反汇编，不是猜**：``AllianceNetWorkModule.NewPitPersonalLimit
#: .IsAchieveLimit(RoleMain)``（Offset ``0x20C40A0``）按 ``m_pitLimitType`` 分派，
#: 前四类（资质/元素/职业/种族）比完是 ``jne → 返回 false``（**相等**），
#: 后三类（星级/等级/品质）是 ``jl → 返回 false``（**大于等于**）。
#:
#: ``None`` 表示该条件不从 :class:`RoleAttributes` 取，而是从名册的
#: ``RoleClass`` 取（星级/等级是**实例**属性，不是形态属性）。
_CONDITION_RULES: Final[dict[str, tuple[str | None, str]]] = {
    "qualification": ("qualification_print", "eq"),
    "element": ("element_id", "eq"),
    "job": ("job_id", "eq"),
    "race": ("race_id", "eq"),
    "star": (None, "ge"),
    "lv": (None, "ge"),
    "quality": ("quality", "ge"),
}


@dataclass(frozen=True)
class PitConditionTerm:
    """一条开矿条件（某坑"需要 N 个角色满足 X"）。

    :ivar kind: 条件类型，取值见 :data:`PIT_CONDITION_KINDS`。
    :ivar value: 条件值。元素是 ``elementID``（如 138000003）、资质是
        ``qualificationPrint``（如 23）、职业是 ``jobID``（如 900000001）、
        种族是 ``RaceData.id``（如 900001001）、星级/等级/品质是**下限**。
    :ivar need: 需要几个角色满足它（``*ConditionNumber``）。

    .. note::
       ⚠️ **元素比的是 ``elementID`` 而不是 ``elementType``** ——
       ``IsAchieveLimit`` 读的是 ``RoleElementData+0x10``（elementID）。
       矿点表里的 ``elementCondition`` 给的也正是 ``138000001~138000007``
       这些基础 elementID。拿 elementType 去比会**永远不匹配**。
    """

    kind: str
    value: int
    need: int = 1

    @property
    def label(self) -> str:
        """可读描述（报缺文案用）。

        【为什么元素要带中文名】报缺文案里写 ``元素=138000003`` 用户读不懂 ——
        而"缺哪个元素"恰恰是他决定去练哪个角色的唯一依据。所以元素翻成
        「水（138000003）」；保留 ID 是为了能跟表里的值直接对上。
        """
        name = PIT_CONDITION_LABELS.get(self.kind, self.kind)
        if self.kind in ("star", "lv", "quality"):
            return f"{name}≥{self.value}"
        element_name = BASE_ELEMENT_NAMES.get(self.value) if self.kind == "element" else None
        if element_name:
            return f"{name}={element_name}（{self.value}）"
        return f"{name}={self.value}"

    def matches(
        self,
        *,
        attributes: RoleAttributes | None,
        level: int,
        star: int,
    ) -> bool:
        """这个角色是否满足本条件。

        :param attributes: 角色形态属性（``load_role_attributes()`` 的产物）。
            条件需要它而它为 ``None`` 时**一律返回 False**（宁可判"不满足"，
            也不要凭空放行一个可能不满足的角色 —— 服务端会拒整批）。
        :param level: 角色等级（``RoleClass.roleLV``）。
        :param star: 角色星级（``RoleClass.roleStar``）。
        """
        field, compare = _CONDITION_RULES.get(self.kind, (None, "eq"))
        if field is None:
            # 星级 / 等级：来自名册实例
            actual = star if self.kind == "star" else level
        else:
            if attributes is None:
                return False
            actual = getattr(attributes, field, None)
            if actual is None or actual == NO_REFERENCE:
                return False
        if compare == "ge":
            return actual >= self.value
        return actual == self.value


@dataclass(frozen=True)
class PersonalPitCondition:
    """一个个人矿点模板的开矿条件（``AlliancePersonalPitData`` 的一行）。

    :ivar personal_pit_id: 矿点模板 ID（21040 ``personalList[].id``，9350001xx 段）。
    :ivar role_max: 该坑**最多可指派的角色数**（``roleMaxCondition``）。
        10.3 抓包里客户端填进 ``roleSidList`` 的人数与它逐一相等
        （模板 113/114→2、124→4、127→5、131/132/133/140→6）。
    :ivar title: 模板名（日志展示用）。
    :ivar terms: 全部角色筛选条件。**空元组 = 没有条件**（构造夹具时的默认值）。

    .. note::
       140 行模板里 **140 行都要求元素 + 资质**，84 行还要求职业、64 行要求星级、
       50 行要求品质、5 行要求种族（``lvCondition`` 本作没用上）。
       所以"只按等级从高到低挑人"必然被服务端拒 —— 那是 21047 回 ``-2`` 的根因。
    """

    personal_pit_id: int
    role_max: int
    title: str = ""
    terms: tuple[PitConditionTerm, ...] = ()

    @property
    def terms_by_kind(self) -> dict[str, tuple[PitConditionTerm, ...]]:
        """按类型分组（客户端 ``m_dic_newPitLimit_Personal`` 的口径）。"""
        grouped: dict[str, list[PitConditionTerm]] = {}
        for term in self.terms:
            grouped.setdefault(term.kind, []).append(term)
        return {kind: tuple(items) for kind, items in grouped.items()}

    def describe_terms(self) -> str:
        """把条件渲染成一行（报缺文案用）；无条件时返回 ``"无条件"``。"""
        if not self.terms:
            return "无条件"
        return "、".join(term.label for term in self.terms)


def _condition_values(row: TableRow, column: str) -> tuple[int, ...]:
    """读一列 ``$`` 分隔的多值条件（``-1``/空 表示没有这条条件）。"""
    raw = row.get(column)
    if raw is None:
        return ()
    values: list[int] = []
    for token in str(raw).split("$"):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(values)


def _condition_counts(row: TableRow, column: str, *, expect: int) -> tuple[int, ...]:
    """读条件对应的"需要几个"（``*ConditionNumber``），长度对齐到 ``expect``。"""
    raw = row.get(column)
    counts: list[int] = []
    if raw is not None:
        for token in str(raw).split("$"):
            token = token.strip()
            try:
                counts.append(int(token))
            except ValueError:
                counts.append(1)
    counts = [count for count in counts if count > 0]
    if not counts:
        counts = [1] * expect
    while len(counts) < expect:
        counts.append(counts[-1])
    return tuple(counts[:expect])


def load_personal_pit_conditions(
    path: Path | None = None,
) -> dict[int, PersonalPitCondition]:
    """读取个人矿点模板条件（``AlliancePersonalPitData``，含全部 7 类筛选条件）。

    :return: ``{personalPitID: PersonalPitCondition}``。
    :raises TableError: 某行缺少主键 ``personalPitID`` 时。

    .. note::
       多值条件（如 ``elementCondition=138000001$138000002`` 配
       ``elementConditionNumber=1$1``）会**拆成多条** :class:`PitConditionTerm`，
       与客户端"每个取值一条限制"的建模一致。
    """
    rows = read_table(path or table_path(TABLE_PERSONAL_PIT))
    conditions: dict[int, PersonalPitCondition] = {}
    for row in rows:
        pit_id = to_int(row, "personalPitID")
        if pit_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 personalPitID")
        terms: list[PitConditionTerm] = []
        for kind in PIT_CONDITION_KINDS:
            values = _condition_values(row, f"{kind}Condition")
            if not values:
                continue
            counts = _condition_counts(
                row, f"{kind}ConditionNumber", expect=len(values)
            )
            for value, need in zip(values, counts):
                terms.append(PitConditionTerm(kind=kind, value=value, need=need))
        conditions[pit_id] = PersonalPitCondition(
            personal_pit_id=pit_id,
            role_max=_int_or(row, "roleMaxCondition", 0),
            title=to_text(row, "title") or "",
            terms=tuple(terms),
        )
    return conditions


# ---------------------------------------------------------------------------
# 工会团体矿点（AllianceTeamPitData）
#   —— 占坑**排序**的难度依据（占坑请求本身不含这些数据，见下方说明）
# ---------------------------------------------------------------------------
#: 团体矿点模板表（``Cherrytale Asset/TextAsset/AllianceTeamPitData``）
TABLE_TEAM_PIT: Final[str] = "AllianceTeamPitData"


@dataclass(frozen=True)
class TeamPitCondition:
    """一个团体矿点模板的静态属性（``AllianceTeamPitData`` 的一行）。

    :ivar team_pit_id: 矿点模板 ID（21040 ``teamList[].id``，9360000xx 段）。
    :ivar difficulty: 难度（1~5）。**这是本类存在的理由** —— 占坑排队时
        "同空槽数下先占奖励多的"需要一个"奖励多少"的代理量，难度就是它。
    :ivar role_max: 该坑的槽位数（``roleMaxCondition``）。10.3 抓包 18/18
        实测槽位数与它严格相等，且只由难度决定（1→2、2→3、3→4、4→5、5→6）。
    :ivar group_percent: 分组权重（语义待实测，本模块不消费）。
    :ivar title: 模板名（日志展示用，如"牛刀小試""縱橫捭闔"）。

    .. note::
        与 :class:`PersonalPitCondition` 不同 —— 个人矿的模板表决定**请求形状**
        （每坑填几个角色，读不到就不该发包）；团体矿占坑请求（21043）只带
        ``pitSid`` / ``roleSidList`` / ``teamPitIndex``，**不含模板数据**，
        所以本表读不到只是"排序退化"，不阻断占坑。
    """

    team_pit_id: int
    difficulty: int = 0
    role_max: int = 0
    group_percent: int = 0
    title: str = ""


def load_team_pit_conditions(
    path: Path | None = None,
) -> dict[int, TeamPitCondition]:
    """读取团体矿点模板表（``AllianceTeamPitData``）。

    :return: ``{teamPitID: TeamPitCondition}``。
    :raises TableError: 某行缺少主键 ``teamPitID`` 时。
    """
    rows = read_table(path or table_path(TABLE_TEAM_PIT))
    conditions: dict[int, TeamPitCondition] = {}
    for row in rows:
        pit_id = to_int(row, "teamPitID")
        if pit_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 teamPitID")
        conditions[pit_id] = TeamPitCondition(
            team_pit_id=pit_id,
            difficulty=_int_or(row, "difficulty", 0),
            role_max=_int_or(row, "roleMaxCondition", 0),
            group_percent=_int_or(row, "groupPersent", 0),
            title=to_text(row, "title") or "",
        )
    return conditions


# ---------------------------------------------------------------------------
# 角色 ID 换算：RoleMainID(130xxxxxx) ↔ RoleInfoID(133xxxxxx)
#   —— ★ 工会团体挖矿「名册中没有可用的匹配」的根因就在这两个 ID 空间
# ---------------------------------------------------------------------------
#: 角色主表（``RoleMainData``，实测 6653 行，主键 ``roleMainID`` = 130000001~130954009）
TABLE_ROLE_MAIN: Final[str] = "RoleMainData"

#: 角色信息表（``RoleInfoData``，实测 587 行，主键 ``roleID`` = 133000001~133991800）
TABLE_ROLE_INFO: Final[str] = "RoleInfoData"

#: ``RoleInfoData.icon1_AssetId`` 的固定后缀（如 ``a001_01_Icon``）
_ICON_ASSET_SUFFIX: Final[str] = "_Icon"

#: 现役主头像（"hero 图"）文件名比配置表裸名多出的后缀
#:
#: 【★ 2026-10-06 实测：这是头像显示的头号坑】
#: 仓库静态目录里的文件叫 ``a001_03h.png``，而配置表给的是：
#:   * ``RoleMainData.roleModelID``   = ``a001_03``      ← 少一个 h
#:   * ``RoleInfoData.icon1_AssetId`` = ``a001_01_Icon`` ← 去尾后少一个 h
#:   * ``ItemData.iconAssetID``       = ``e001_01h_Icon``← **只有它带 h**
#: 所以想按 RoleMainID 找头像，**必须自己补 h**；这也解释了
#: ``services/avatar_service.py::hero_code_from_ids()`` 那条 roleModelID 兜底链
#: 为什么从未命中过（它拿 ``a001_03`` 去找 ``a001_03.png``）。
_AVATAR_HERO_SUFFIX: Final[str] = "h"


class RoleIdMap:
    """把 ``RoleClass.roleID``（**RoleMainID**）换算成矿点要的 **RoleInfoID**。

    【为什么必须有它（★ 2026-10-05 反汇编实证）】

    ``RoleClass.roleID`` 是 **RoleMainID**（130xxxxxx 段），而
    ``AllianceTeamPitDetailClass.needRoleInfoID`` 是 **RoleInfoID**（133xxxxxx 段）。
    拿它们直接比 ``==`` **永远为假** —— 这就是「团体矿永远报
    『名册中没有可用的匹配』」的根因（**不是名册没拉到**）。

    反汇编依据：``RoleMainModule.AddServerRole``（Offset ``0x19C2040``）把
    ``RoleClass+0x14``(roleID) 交给 ``isExistInHashSet(roleID, this+0x60)``，
    而 ``RoleMainModule`` 字段表里 ``0x60 = m_hashSet_RoleMainID``
    （``0x70`` 才是 ``m_hashSet_RoleInfoID``）。

    【换算链】

    ``roleMainID`` 沿 ``RoleMainData.qualityUpRoleSubID`` **反向回溯到链首**
    （= 该角色的基础形态），再用 ``RoleInfoData.initialRoleMainID`` 反查 RoleInfoID::

        130003002 (品质 3 形态)
          → base 130003000
          → RoleInfoData[initialRoleMainID == 130003000].roleID = 133003000

    品质升级形态（130003001 / 130003002 / …）**都换算到同一个 RoleInfoID**，
    所以匹配时"任一命中即可"。

    .. note::
       客户端自己的实现是 ``GetRoleInfoID_ByRoleMainID``，它读的是
       ``RoleMainData.info``(偏移 0x20) → ``RoleInfoData.roleID``(偏移 0x10)。
       但 ``RoleMainData.info`` 这一列在**文本配置表里全是 NULL**
       （运行时由二进制配置补上），所以本项目改用上面那条链重建 ——
       离线验证：日志里 7 个空槽的 ``needRoleInfoID`` **7/7 可被命中**。

    .. note::
       一个 RoleInfoID 可以对应**多个** RoleMainID（同角色的不同品质形态），
       反之一个 RoleMainID 也可能对应多个 RoleInfoID（觉醒/异形角色，
       例：``initialRoleMainID=130000100`` 同时挂在 133000005 / 133000014 / 133000100 上）。
       所以 :meth:`role_info_ids` 返回的是**元组**而不是单值。

    .. note::
       **本类不负责元素 / 职业 / 资质**。那些条件在 ``RoleMainData.element``
       （文本表里为 NULL）、``RoleInfoData.attributeType``（141000001 力量 /
       142000002 智力，**不是元素**）等处 —— 元素/资质/职业/种族由
       :class:`RoleAttributes` 经 ``DataRelative`` 索引补齐，见
       ``notes/personal_mining_conditions_plan.md``。

    .. note::
       **★ 2026-10-06 修正**：``RoleMainData.info`` 在文本表里确实全是 NULL
       （这样客户端才需要 ``GetRoleInfoID_ByRoleMainID``），但
       ``RoleMainData.roleModelID`` **6653 行全部有值**（旧注释误记成"几乎全表
       NULL，只有默认剪影那行有效"）。它是头像编号的裸名，比实际文件名少一个
       ``h`` —— 用 :meth:`role_icon_codes` 取，别自己拼。
    """

    __slots__ = ("_parent", "_info_of_base", "_name_of_info", "_icon_of_main", "_icon_of_info")

    def __init__(
        self,
        *,
        parent: Mapping[int, int],
        info_of_base: Mapping[int, Sequence[int]],
        name_of_info: Mapping[int, str],
        icon_of_main: Mapping[int, str] | None = None,
        icon_of_info: Mapping[int, str] | None = None,
    ) -> None:
        #: ``{子形态 roleMainID: 父形态 roleMainID}``（由 qualityUpRoleSubID 反建）
        self._parent: Mapping[int, int] = dict(parent)
        #: ``{initialRoleMainID: (roleID, ...)}``
        self._info_of_base: Mapping[int, tuple[int, ...]] = {
            key: tuple(value) for key, value in info_of_base.items()
        }
        #: ``{roleID: name}``
        self._name_of_info: Mapping[int, str] = dict(name_of_info)
        #: ``{roleMainID: roleModelID}``（头像编号裸名，如 ``a001_03``；★ 不带 h）
        self._icon_of_main: Mapping[int, str] = dict(icon_of_main or {})
        #: ``{roleInfoID: icon1_AssetId}``（已去 ``_Icon`` 尾，如 ``a001_01``；★ 不带 h）
        self._icon_of_info: Mapping[int, str] = dict(icon_of_info or {})

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_tables(
        cls,
        *,
        main_path: Path | None = None,
        info_path: Path | None = None,
    ) -> RoleIdMap:
        """从两张配置表建映射。

        :param main_path: ``RoleMainData`` 路径（``None`` = 走 ``table_path``）。
        :param info_path: ``RoleInfoData`` 路径（``None`` = 走 ``table_path``）。
        :raises TableError: 缺主键时。
        """
        parent: dict[int, int] = {}
        icon_of_main: dict[int, str] = {}
        for row in read_table(main_path or table_path(TABLE_ROLE_MAIN)):
            main_id = to_int(row, "roleMainID")
            if main_id is None:
                raise TableError(row.path, row.line_no, "缺少主键 roleMainID")
            # qualityUpRoleSubID = -1 表示"没有更高品质形态"（链尾）
            sub_id = _int_or(row, "qualityUpRoleSubID", -1)
            if sub_id > 0:
                parent[sub_id] = main_id
            # ★ 2026-10-06：roleModelID 是**头像编号的裸名**（如 ``a001_03``），
            #   实际文件名要在末尾补 ``h``（``a001_03h.png``）—— 见
            #   :meth:`role_icon_codes` 的说明。这里只做原样记录，不做加工。
            model = (to_text(row, "roleModelID") or "").strip()
            if model and model.upper() != "NULL":
                icon_of_main[main_id] = model

        info_of_base: dict[int, list[int]] = {}
        name_of_info: dict[int, str] = {}
        icon_of_info: dict[int, str] = {}
        for row in read_table(info_path or table_path(TABLE_ROLE_INFO)):
            info_id = to_int(row, "roleID")
            if info_id is None:
                raise TableError(row.path, row.line_no, "缺少主键 roleID")
            name_of_info[info_id] = to_text(row, "name") or ""
            # initialRoleMainID = -1 是"剧情/怪物"等非玩家角色，不进映射
            base_id = _int_or(row, "initialRoleMainID", -1)
            if base_id > 0:
                info_of_base.setdefault(base_id, []).append(info_id)
            # ★ 2026-10-06：icon1_AssetId（如 ``a001_01_Icon``）——去掉固定后缀
            #   ``_Icon`` 得到裸名（``a001_01``），同样需要补 ``h``。
            asset = (to_text(row, "icon1_AssetId") or "").strip()
            if asset.endswith(_ICON_ASSET_SUFFIX):
                icon_of_info[info_id] = asset[: -len(_ICON_ASSET_SUFFIX)]

        return cls(
            parent=parent,
            info_of_base=info_of_base,
            name_of_info=name_of_info,
            icon_of_main=icon_of_main,
            icon_of_info=icon_of_info,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def base_main_id(self, role_main_id: int) -> int:
        """沿品质升级链回溯到链首（基础形态的 roleMainID）。

        :return: 链首的 roleMainID；入参不在表里时**原样返回**（保守：
            宁可换算不出来，也不要凭空换成一个别的角色）。
        """
        current = role_main_id
        seen: set[int] = set()
        while current in self._parent and current not in seen:
            seen.add(current)
            current = self._parent[current]
        return current

    def role_info_ids(self, role_main_id: int) -> tuple[int, ...]:
        """该 roleMainID 对应的全部 RoleInfoID（可能多个，见类 docstring）。"""
        return self._info_of_base.get(self.base_main_id(role_main_id), ())

    def matches(self, role_main_id: int, need_role_info_id: int) -> bool:
        """``RoleClass.roleID`` 是否满足矿点要求的 ``needRoleInfoID``。

        **这是团体矿挑角色的正确判据**（替代过去那个永远为假的
        ``role.roleID == slot.needRoleInfoID``）。
        """
        if need_role_info_id <= 0:
            return False
        return need_role_info_id in self.role_info_ids(role_main_id)

    def name_of(self, role_main_id: int) -> str:
        """角色中文名（取自 ``RoleInfoData.name``）；查不到返回空串。

        .. note::
           一个 roleMainID 可能对应多个 RoleInfoID（觉醒形态），
           此时取**第一个**有名字的 —— 展示用足够，别拿它做匹配。
        """
        for info_id in self.role_info_ids(role_main_id):
            name = self._name_of_info.get(info_id, "")
            if name:
                return name
        return ""

    def name_of_info(self, role_info_id: int) -> str:
        """按 **RoleInfoID** 取角色中文名（矿点要求里给的就是它）。

        与 :meth:`name_of` 的区别：那个收的是 RoleMainID（名册里给的），
        这个收的是 RoleInfoID（``needRoleInfoID`` 给的）。
        查不到返回空串 —— 调用方应把它渲染成"未知角色"，不要编名字。
        """
        return self._name_of_info.get(role_info_id, "")

    # ------------------------------------------------------------------
    # 头像（★ 2026-10-06 新增）
    # ------------------------------------------------------------------
    def role_icon_codes(self, role_main_id: int) -> tuple[str, ...]:
        """该角色**可能的头像文件名裸名**（按优先级，**不含 h 后缀**）。

        【它解决什么】「我的角色」面板要给每个角色配头像。名册里只有 RoleMainID，
        而静态目录 ``web/assets/avatars/`` 的文件名是 ``a001_03h.png`` 这种形态
        —— 配置表给的裸名比它少一个 ``h``（见 :data:`_AVATAR_HERO_SUFFIX`）。

        【★ 为什么只用 ``RoleInfoData.icon1_AssetId``，不用 ``roleModelID``】
        2026-10-06 全量实测（见 ``notes/role_avatar_display_plan.md``）：

        ``RoleMainData.roleModelID`` 补 ``h`` 后有 **28 处张冠李戴**
        —— 例如 ``roleMainID=130000100`` 名字是**黃金騎士**，而它的
        ``roleModelID=e001_01`` 补 h 后指向 ``e001_01h``（**仙度瑞拉**）。
        根因：一个 ``initialRoleMainID`` 可挂**多个 RoleInfoID**（早期合成角色，
        如 130000100 挂着 133000005/133000014/133000100），而
        :meth:`name_of` 取的是**第一个** RoleInfoID，``roleModelID`` 却可能
        对应链上**另一个**节点 —— 名字与头像来自不同节点，必然对不齐。

        所以本方法**以 ``RoleInfoData.icon1_AssetId`` 为唯一来源**，并且
        **优先取与 :meth:`name_of` 同名的那个 RoleInfoID**（名字与图标来自
        同一条记录，天然自洽）。实测效果：命中率 82.7%（对比 roleModelID 路线的
        83.0%，**只少 7 个**），但**名字冲突从 28 降到 0** —— 少 7 张图换零错配，
        这笔交易划算（错配的图比缺图更有害：用户会以为角色练错了）。

        :param role_main_id: 名册里的 ``roleID``（= RoleMainID，130xxxxxx）。
        :return: 去重后的候选裸名（**保序**）；都查不到返回空元组。

        .. note::
           裸名**必须再由调用方补 ``h`` 并做存在性校验** —— 因为
           ① 旧版/非现役头像在静态目录里也可能是不带 h 的名字；
           ② 猜出来的名字如果文件不存在，显示破图比不显示更糟。
           本项目在 ``services/avatar_service.py::resolve_role_icon_code()`` 里
           统一做这件事。
        """
        info_ids = self.role_info_ids(role_main_id)
        if not info_ids:
            return ()
        # 只在"与角色名同名"的那些 RoleInfoID 里找 —— 名字与图标同源，最可信。
        #
        # 【为什么不拿同链其他 RoleInfoID 兜底】实测 ``130000300``（炎木樹魔）
        # 自己的 icon1（``b001_01``）在静态目录里没有图；若允许兜底到同链的
        # ``133000300``（費妲，``c001_03h``），炎木樹魔就会**顶着費妲的头像**。
        # 那是"用错图"而不是"没图" —— 用户会以为角色练错了，比空白更有害。
        # 所以宁可不返回，让调用方走统一占位图。
        target = self.name_of(role_main_id)
        codes: list[str] = []
        for info_id in info_ids:
            if target and self._name_of_info.get(info_id) != target:
                continue
            code = self._icon_of_info.get(info_id, "")
            if code:
                codes.append(code)
        # 去重保持顺序（dict.fromkeys 是保序去重的标准写法）
        return tuple(dict.fromkeys(codes))

    def icon_code_of_main(self, role_main_id: int) -> str:
        """``RoleMainData.roleModelID`` 裸名（如 ``a001_03``）；没有返回空串。

        .. warning::
           **不要拿它当头像来源** —— 它有 28 处与角色名不符（见
           :meth:`role_icon_codes` 的说明）。保留本方法只为诊断/排错。
        """
        return self._icon_of_main.get(role_main_id, "")


    def icon_code_of_info(self, role_info_id: int) -> str:
        """``RoleInfoData.icon1_AssetId`` 去 ``_Icon`` 后的裸名；没有返回空串。"""
        return self._icon_of_info.get(role_info_id, "")

    def main_ids_for_info(self, role_info_id: int) -> tuple[int, ...]:
        """反查：所有能换算到该 RoleInfoID 的 roleMainID（含品质升级形态）。

        用于诊断与测试（"这个矿点要求的角色，我到底有没有"）。
        """
        base = None
        for candidate, info_ids in self._info_of_base.items():
            if role_info_id in info_ids:
                base = candidate
                break
        if base is None:
            return ()
        found = {base}
        # 沿 parent 反向展开：所有以 base 为链首的形态
        for child, parent_id in self._parent.items():
            if parent_id == base:
                found.add(child)
                # 链可能不止两层，继续往下展开
                frontier = [child]
                while frontier:
                    node = frontier.pop()
                    for grand_child, node_parent in self._parent.items():
                        if node_parent == node and grand_child not in found:
                            found.add(grand_child)
                            frontier.append(grand_child)
        return tuple(sorted(found))

    def __len__(self) -> int:
        """可换算的 roleMainID 条数（诊断用）。"""
        return len(self._parent) + len(self._info_of_base)


def load_role_id_map(
    *,
    main_path: Path | None = None,
    info_path: Path | None = None,
) -> RoleIdMap:
    """读取 ``RoleMainData`` + ``RoleInfoData``，构造 :class:`RoleIdMap`。

    与其它 ``load_*`` 一致：**每次调用都重新读表**（不做全局缓存）——
    配置表是可以被换掉的素材，缓存会带来"改了表但内存里还是旧值"这种更难查的问题。
    """
    return RoleIdMap.from_tables(main_path=main_path, info_path=info_path)


# ---------------------------------------------------------------------------
# 表间引用索引（DataRelative）★ 文本导出里被抹空的引用列，全在这儿
# ---------------------------------------------------------------------------
#: 表间引用索引表（``DataRelative``，3.9 MB / 987 行）。
#:
#: 【★★ 2026-10-06：这张表之前被整张跳过了，是个大坑】
#: 它**不是普通配置表** —— 是配置系统用来解析引用列的反查索引。格式是
#: **每两条一组**：第 1 行是关系声明 ``源表|字段|目标表|目标字段``，
#: 第 2 行是**逐行对齐源表**的引用值（``|`` 分隔）。
#: 所以按"统一列数"去解析它必然失败 —— 之前正是因此把它当成"没有表头的
#: 巨型 blob"跳过了，连带得出了"角色的元素/资质拿不到"的**错误结论**。
#:
#: 实测：``RoleMainData.element`` 在 ``RoleMainData`` 里 6653 行**全为 NULL**
#: （文本导出把引用列抹空了），但在这张表里**一个不缺**。
TABLE_DATA_RELATIVE: Final[str] = "DataRelative"

#: 角色**形态**（RoleMainData 的一行）→ 元素（RoleElementData）
REL_ROLE_MAIN_ELEMENT: Final[str] = "RoleMainData|element|RoleElementData|elementID"
#: 角色形态 → 资质（RoleQualificationData）
REL_ROLE_MAIN_QUALIFICATION: Final[str] = (
    "RoleMainData|roleQualification|RoleQualificationData|roleQualificationID"
)
#: 角色形态 → 角色（RoleInfoData）。用来把 job/race 从 RoleInfoData 接过来。
REL_ROLE_MAIN_INFO: Final[str] = "RoleMainData|info|RoleInfoData|roleID"
#: 角色 → 职业（JobData）
REL_ROLE_INFO_JOB: Final[str] = "RoleInfoData|jobType|JobData|jobID"
#: 角色 → 种族（RaceData）
REL_ROLE_INFO_RACE: Final[str] = "RoleInfoData|race|RaceData|id"

#: 引用索引里表示"没有引用"的哨兵（表里写的就是 ``-1``）
NO_REFERENCE: Final[int] = -1


def load_data_relative(path: Path | None = None) -> dict[str, tuple[int, ...]]:
    """读取表间引用索引（``DataRelative``）。

    :return: ``{关系声明: (值, …)}``，例如
        ``{"RoleMainData|element|RoleElementData|elementID": (138000006, 138000004, …)}``。
        **值的顺序与源表的行序一致** —— 这是它唯一的用法。
    :raises TableError: 关系声明重复时（表被改坏）。

    【为什么按"每两条一组"解析，而不是按列数】
    见 :data:`TABLE_DATA_RELATIVE` —— 它不是普通表。判定规则：
    **一行 4 个字段、且第 1 个字段是表名（非纯数字）** ⇒ 它是关系声明，
    紧随其后的一行就是它的值列表。

    .. note::
       值列表里解析不出整数的项一律记 :data:`NO_REFERENCE`，**保留位置** ——
       这条表的价值全在"第 i 个值对应源表第 i 行"，丢位置比丢值严重得多。
    """
    target = path or table_path(TABLE_DATA_RELATIVE)
    try:
        text = target.read_text(encoding=TABLE_ENCODING)
    except OSError as exc:
        raise TableError(target, 0, f"引用索引表读取失败：{exc}") from exc

    lines = text.split("\n")
    relations: dict[str, tuple[int, ...]] = {}
    index = 0
    while index < len(lines) - 1:
        head = lines[index].strip()
        fields = head.split("|")
        if len(fields) == 4 and not fields[0].lstrip("-").isdigit():
            values: list[int] = []
            for token in lines[index + 1].split("|"):
                token = token.strip()
                if not token:
                    continue
                try:
                    values.append(int(token))
                except ValueError:
                    values.append(NO_REFERENCE)
            if head in relations:
                raise TableError(target, index + 1, f"关系声明重复：{head}")
            relations[head] = tuple(values)
            index += 2
            continue
        index += 1
    return relations


def _relation_aligned(
    relations: Mapping[str, Sequence[int]],
    key: str,
    *,
    source_rows: int,
    source_name: str,
) -> tuple[int, ...]:
    """取一条关系并校验它与源表**行数一致**。

    :raises TableError: 关系缺失，或值数与源表行数不符。

    【为什么要校验】这条表的价值就是"逐行对齐"。一旦游戏更新让两边错位，
    我们读到的就是**别的角色的元素** —— 而它看起来完全正常（就是个合法 ID）。
    宁可在这里报错，也不要静默给出错误的编队。
    """
    values = relations.get(key)
    if values is None:
        raise TableError(
            table_path(TABLE_DATA_RELATIVE), 0, f"引用索引里没有关系：{key}"
        )
    if len(values) != source_rows:
        raise TableError(
            table_path(TABLE_DATA_RELATIVE),
            0,
            f"关系 {key} 的值数 {len(values)} 与 {source_name} 的行数 "
            f"{source_rows} 不一致 —— 两边已经错位，不能用",
        )
    return tuple(values)


# ---------------------------------------------------------------------------
# 角色属性（矿点条件的判定依据）
#   —— 元素 / 资质 / 职业 / 种族 都来自 DataRelative（文本表里全是 NULL）
# ---------------------------------------------------------------------------
#: 角色资质表（``RoleQualificationData``）
TABLE_ROLE_QUALIFICATION: Final[str] = "RoleQualificationData"

#: 角色元素表（``RoleElementData``）—— 只用来把元素 ID 翻成中文名
TABLE_ROLE_ELEMENT: Final[str] = "RoleElementData"

#: 基础元素 ID → 中文名（取自 ``RoleElementData.elementName``）。
#:
#: 矿点条件里出现的就是这 7 个基础 ID（``138000001``~``138000007``）。
#: 角色的 ``element_id`` 可能是**变体**（``1381xxxxx``~``1387xxxxx``），
#: 但矿点只认基础 ID，所以这里只列这 7 个。
#: ``tests/test_game_config_pit_conditions.py`` 会拿真实表核对这张常量。
BASE_ELEMENT_NAMES: Final[dict[int, str]] = {
    138000001: "風",
    138000002: "土",
    138000003: "水",
    138000004: "火",
    138000005: "無",
    138000006: "光",
    138000007: "闇",
}


@dataclass(frozen=True)
class RoleAttributes:
    """一个角色**形态**（RoleMainData 的一行）的条件相关属性。

    :ivar element_id: 元素 ID（``RoleElementData.elementID``，如 138000003）。
        ⚠️ 矿点的 ``elementCondition`` 比的就是**这个 ID**（不是 ``elementType``），
        见 :class:`PersonalPitCondition` 的说明。
    :ivar qualification_print: 资质档（``RoleQualificationData.qualificationPrint``，
        取值 17~25）。矿点的 ``qualificationCondition`` 比的是它。
    :ivar job_id: 职业 ID（``JobData.jobID``，如 900000001）。
    :ivar race_id: 种族 ID（``RaceData.id``，如 900001001）。
    :ivar quality: 品质档（``RoleMainData.quality``，0~9）。
    """

    role_main_id: int
    element_id: int = NO_REFERENCE
    qualification_print: int = 0
    job_id: int = NO_REFERENCE
    race_id: int = NO_REFERENCE
    quality: int = 0


def load_role_attributes(
    *,
    relative_path: Path | None = None,
    main_path: Path | None = None,
    info_path: Path | None = None,
    qualification_path: Path | None = None,
) -> dict[int, RoleAttributes]:
    """构造 ``{roleMainID: RoleAttributes}``。

    :raises TableError: 任一引用关系的值数与源表行数不符（两边错位）时。

    【四条数据的来路】

    ====================  =========================================================
    字段                  来源
    ====================  =========================================================
    ``element_id``        ``DataRelative`` 的 ``RoleMainData|element|…``（逐行对齐）
    ``qualification_print`` 同上拿 ``roleQualificationID``，再查
                          ``RoleQualificationData.qualificationPrint``
    ``quality``           ``RoleMainData.quality``（文本表里本来就有）
    ``job_id``/``race_id`` ``DataRelative`` 的 ``RoleMainData|info|RoleInfoData|roleID``
                          先定位到该形态对应的 RoleInfoData，再取
                          ``RoleInfoData|jobType`` / ``RoleInfoData|race``
    ====================  =========================================================
    """
    relations = load_data_relative(relative_path)
    main_rows = read_table(main_path or table_path(TABLE_ROLE_MAIN))
    info_rows = read_table(info_path or table_path(TABLE_ROLE_INFO))

    elements = _relation_aligned(
        relations,
        REL_ROLE_MAIN_ELEMENT,
        source_rows=len(main_rows),
        source_name=TABLE_ROLE_MAIN,
    )
    qualification_ids = _relation_aligned(
        relations,
        REL_ROLE_MAIN_QUALIFICATION,
        source_rows=len(main_rows),
        source_name=TABLE_ROLE_MAIN,
    )
    info_ids = _relation_aligned(
        relations,
        REL_ROLE_MAIN_INFO,
        source_rows=len(main_rows),
        source_name=TABLE_ROLE_MAIN,
    )
    jobs = _relation_aligned(
        relations,
        REL_ROLE_INFO_JOB,
        source_rows=len(info_rows),
        source_name=TABLE_ROLE_INFO,
    )
    races = _relation_aligned(
        relations,
        REL_ROLE_INFO_RACE,
        source_rows=len(info_rows),
        source_name=TABLE_ROLE_INFO,
    )

    # RoleInfoData 的行序 → 它的 roleID（用它把 info 关系对齐到 job/race）
    info_index = {
        to_int(row, "roleID"): position for position, row in enumerate(info_rows)
    }
    qualification_print = {
        to_int(row, "roleQualificationID"): _int_or(row, "qualificationPrint", 0)
        for row in read_table(qualification_path or table_path(TABLE_ROLE_QUALIFICATION))
    }

    attributes: dict[int, RoleAttributes] = {}
    for position, row in enumerate(main_rows):
        main_id = to_int(row, "roleMainID")
        if main_id is None:
            raise TableError(row.path, row.line_no, "缺少主键 roleMainID")
        info_position = info_index.get(info_ids[position], -1)
        attributes[main_id] = RoleAttributes(
            role_main_id=main_id,
            element_id=elements[position],
            qualification_print=qualification_print.get(qualification_ids[position], 0),
            job_id=jobs[info_position] if info_position >= 0 else NO_REFERENCE,
            race_id=races[info_position] if info_position >= 0 else NO_REFERENCE,
            quality=_int_or(row, "quality", 0),
        )
    return attributes


# ---------------------------------------------------------------------------
# 降临／活动关卡（SpecialArea / SpecialSection）
#   —— 体力扫荡板块的**屏蔽依据**（"哪些关卡能扫"只有一个权威来源）
# ---------------------------------------------------------------------------
#: 活动区域表（实测 379 行）
TABLE_SPECIAL_AREA: Final[str] = "SpecialAreaData"

#: 活动关卡表（实测 1633 行）
TABLE_SPECIAL_SECTION: Final[str] = "SpecialSectionData"

#: 区域标签表（实测 11 行）
TABLE_SPECIAL_AREA_TAG: Final[str] = "SpecialAreaTagData"

#: 标签「降臨活動」（限时轮换的那一类 —— 需求里说的"隔时间要换掉的降临关卡"）
TAG_ADVENT_ACTIVITY: Final[int] = 901000002

#: 标签「常駐降臨」（不轮换）
TAG_PERMANENT_ADVENT: Final[int] = 901000009

#: ``SpecialSectionData.sweepOff`` 的「**可以**扫荡」取值。
#:
#: 实测分布：``-1`` 共 1004 行、``1`` 共 629 行 —— 也就是说 **-1 才是"允许"**，
#: 而不是"1 = 允许"（写成布尔语义会正好反掉，这是本表最容易踩的坑）。
SWEEP_OFF_ALLOWED: Final[int] = -1


@dataclass(frozen=True)
class SpecialSection:
    """一条活动关卡配置（``SpecialSectionData`` 的一行）。"""

    #: ``sectionID`` —— 关卡 ID（``11003`` 的返回与 ``11009`` 的请求都用它）
    section_id: int
    #: ``sectionName`` —— 关卡名（实测形如 ``關卡9``）
    section_name: str | None
    #: ``energyItemRequir`` —— 消耗的道具 ID（实测 ``200000002`` = 体力）
    energy_item_id: int
    #: ``energyCost`` —— **每次扫荡消耗的体力**（实测第 9 关是 40）
    energy_cost: int
    #: ``sweepOff`` —— 配置表是否禁止扫荡（``-1`` = 允许，其它值 = 禁止）
    sweep_off: int
    #: ``sweepRequireItemAmount`` —— 每次扫荡需要的券数（实测**全表都是 1**）
    sweep_require_item_amount: int
    #: ``cheatCheck`` —— 是否开启反外挂校验（实测 1435 行为 ``0``、198 行为 ``1``）
    cheat_check: int
    #: ``limitItemMax`` —— 限次道具上限（``-1`` = 不限）
    limit_item_max: int
    #: ``bonusGroupExp`` —— 组队经验加成
    bonus_group_exp: int
    #: ``bonusExp`` —— 经验加成
    bonus_exp: int
    #: 原始行（将来要用到别的列时不必改本类）
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号（报错定位用）。"""
        return self.raw.line_no

    def describe(self) -> str:
        """一行摘要（形如 ``129110539 關卡9（体力 40）``）。"""
        return (
            f"{self.section_id} {self.section_name or '<无名>'}"
            f"（体力 {self.energy_cost}）"
        )


@dataclass(frozen=True)
class SpecialArea:
    """一个活动区域配置（``SpecialAreaData`` 的一行）。"""

    #: ``areaID`` —— 区域 ID（``11026`` / ``11003`` 用它索引）
    area_id: int
    #: ``areaName`` —— 区域名（实测形如 ``可疑的工作``）
    area_name: str | None
    #: ``areaType`` —— 区域类型（实测取值 0/1/2/5，语义未确认，只做展示）
    area_type: int
    #: ``activityScheduleType`` —— 排期类型（实测 1 占多数、3 有 7 行）
    activity_schedule_type: int
    #: ``wipeoutAble`` —— **该区域整体可否扫荡**（实测 375 行为 ``1``、4 行为 ``0``）
    wipeout_able: int
    #: ``rewardObj`` —— 奖励展示串（实测 ``200000001$200300339``）
    reward_obj: str | None
    #: ``priority`` —— 列表排序权重（实测 1002 / 3000 / 8000 …）
    priority: int
    #: ``tagID`` —— ⚠️ **实测该列在数据里全为空**（0 行非空），
    #: 因此"降臨活動/常駐降臨"的标签只能靠 :data:`TAG_ADVENT_ACTIVITY`
    #: 这类编号在别处匹配；本字段只原样保留，不做判断依据。
    tag_id: int | None
    #: 原始行
    raw: TableRow = field(compare=False, repr=False)

    @property
    def line_no(self) -> int:
        """该行在配置文件中的行号（报错定位用）。"""
        return self.raw.line_no

    @property
    def is_wipeout_able(self) -> bool:
        """该区域是否整体允许扫荡。"""
        return self.wipeout_able == 1

    def describe(self) -> str:
        """一行摘要（形如 ``128110053 可疑的工作``）。"""
        return f"{self.area_id} {self.area_name or '<无名>'}"


# ---------------------------------------------------------------------------
# 加载与索引
# ---------------------------------------------------------------------------
def load_special_sections(path: Path | None = None) -> list[SpecialSection]:
    """读取全部活动关卡配置（``SpecialSectionData``，实测 1633 条）。

    :param path: 配置表路径；``None`` 表示自动定位到素材目录下的同名文件。
    :raises TableError: 某行缺少主键 ``sectionID`` 时（带文件名与行号）。
    """
    target = path or table_path(TABLE_SPECIAL_SECTION)
    sections: list[SpecialSection] = []
    for row in read_table(target):
        section_id = to_int(row, "sectionID")
        if section_id is None:
            raise TableError(
                target, row.line_no, "缺少主键 sectionID（本表靠它唯一标识一个关卡）"
            )
        sections.append(
            SpecialSection(
                section_id=section_id,
                section_name=to_text(row, "sectionName"),
                energy_item_id=_int_or(row, "energyItemRequir", 0),
                energy_cost=_int_or(row, "energyCost", 0),
                # ⚠️ 空值按「**禁止**」处理（fail-closed）：sweepOff 的语义是
                #    "-1 = 允许"，一旦这列将来被改动/读空，默认允许就等于放行，
                #    而放行的代价是白扣体力去扫一个不该扫的关卡。
                sweep_off=_int_or(row, "sweepOff", 1),
                sweep_require_item_amount=_int_or(row, "sweepRequireItemAmount", 1),
                # 同理：校验开关读不到时按「开了校验」处理，宁可少扫也不冒险
                cheat_check=_int_or(row, "cheatCheck", 1),
                limit_item_max=_int_or(row, "limitItemMax", -1),
                bonus_group_exp=_int_or(row, "bonusGroupExp", 0),
                bonus_exp=_int_or(row, "bonusExp", 0),
                raw=row,
            )
        )
    return sections


def load_special_areas(path: Path | None = None) -> list[SpecialArea]:
    """读取全部活动区域配置（``SpecialAreaData``，实测 379 条）。

    :raises TableError: 某行缺少主键 ``areaID`` 时（带文件名与行号）。
    """
    target = path or table_path(TABLE_SPECIAL_AREA)
    areas: list[SpecialArea] = []
    for row in read_table(target):
        area_id = to_int(row, "areaID")
        if area_id is None:
            raise TableError(
                target, row.line_no, "缺少主键 areaID（本表靠它唯一标识一个区域）"
            )
        areas.append(
            SpecialArea(
                area_id=area_id,
                area_name=to_text(row, "areaName"),
                area_type=_int_or(row, "areaType", 0),
                activity_schedule_type=_int_or(row, "activityScheduleType", 0),
                # ⚠️ 同样 fail-closed：只有明确写着 1 才算"可扫荡"
                wipeout_able=_int_or(row, "wipeoutAble", 0),
                reward_obj=to_text(row, "rewardObj"),
                priority=_int_or(row, "priority", 0),
                tag_id=to_int(row, "tagID"),
                raw=row,
            )
        )
    return areas


def special_section_index(sections: list[SpecialSection]) -> dict[int, SpecialSection]:
    """``{sectionID: SpecialSection}``（同名 ID 时后面的覆盖前面的，配置表不该出现）。"""
    return {section.section_id: section for section in sections}


def special_area_index(areas: list[SpecialArea]) -> dict[int, SpecialArea]:
    """``{areaID: SpecialArea}``。"""
    return {area.area_id: area for area in areas}


# ---------------------------------------------------------------------------
# ★ 屏蔽规则：唯一权威实现（任务、测试都调它，不许各写一份）
# ---------------------------------------------------------------------------
def area_block_reason(area: SpecialArea) -> str | None:
    """这个**区域**能不能扫荡？不能则返回原因，能则返回 ``None``。

    :param area: 配置表里的一条活动区域。

    .. note::
       区域级只看 ``wipeoutAble``（实测只有 4 行是 ``0``）。
       关卡级的三条限制见 :func:`sweep_block_reason` —— 两级都要过。
    """
    if not area.is_wipeout_able:
        return f"该区域整体不可扫荡（wipeoutAble={area.wipeout_able}）"
    return None


def area_stamina_sections(
    area_id: int, section_index: dict[int, SpecialSection]
) -> list[SpecialSection]:
    """返回该活动区域在本地配置表中的全部体力关卡（按 sectionID 升序排列）。"""
    aid = str(int(area_id))
    if len(aid) == 9 and aid.startswith("128"):
        p1 = "129" + aid[3:]
        p2 = "129" + aid[3:5] + aid[6:]
        # 优先匹配多关模式 p2（如 129110531~129110539）
        p2_matches = [
            s for sid, s in section_index.items()
            if str(sid).startswith(p2) and s.energy_cost > 0
        ]
        if p2_matches:
            return sorted(p2_matches, key=lambda s: s.section_id)
        p1_matches = [
            s for sid, s in section_index.items()
            if str(sid).startswith(p1) and s.energy_cost > 0
        ]
        if p1_matches:
            return sorted(p1_matches, key=lambda s: s.section_id)
    return []


def area_has_stamina_stages(
    area_id: int, section_index: dict[int, SpecialSection]
) -> bool:
    """该活动区域是否包含消耗体力的关卡？

    用于在区域发现与下拉展示中排除纯 0 体力消耗的关卡/活动区域
    （如角色体验、特别挑战、0 体力 EX 关等）。
    """
    stamina_secs = area_stamina_sections(area_id, section_index)
    if stamina_secs:
        return True
    aid = str(int(area_id))
    if len(aid) == 9 and aid.startswith("128"):
        return False
    return True


def sweep_block_reason(section: SpecialSection) -> str | None:
    """这个**关卡**能不能扫荡？不能则返回原因，能则返回 ``None``。

    三条限制，顺序即优先级（越"根本"的原因越先报）：

    1. **不消耗体力**（``energyCost <= 0``）→ 这是一次性/体验关卡，重复通关没有意义。
       实测这类关卡里仍有 **99 行** 的 ``sweepOff`` 是 ``-1``（配置表并没关掉扫荡），
       所以**必须由我们在客户端挡住** —— 这正是需求里"如果有也请你屏蔽掉"那一条。
    2. **配置表禁止**（``sweepOff != -1``）→ 实测 629 行，尊重配置表。
    3. **开了反外挂校验**（``cheatCheck != 0``）→ 实测 198 行。
       扫荡虽然由服务端结算，但这类关卡本身被标记为需要校验，
       不必要地踩上去风险与收益不成比例 —— 直接不扫，并在日志里说明原因。

    :param section: 配置表里的一条活动关卡。
    """
    if section.energy_cost <= 0:
        return (
            f"不消耗体力的关卡（energyCost={section.energy_cost}）"
            "—— 一次性/体验关，重复通关没有意义"
        )
    if section.sweep_off != SWEEP_OFF_ALLOWED:
        return f"配置表禁止扫荡（sweepOff={section.sweep_off}）"
    if section.cheat_check != 0:
        return f"开启了反外挂校验（cheatCheck={section.cheat_check}），本工具不扫这类关卡"
    return None


def is_sweepable(section: SpecialSection) -> bool:
    """关卡是否**可以**扫荡（= :func:`sweep_block_reason` 返回 ``None``）。"""
    return sweep_block_reason(section) is None


# ---------------------------------------------------------------------------
# 主线关卡（StageSectionData / StageAreaData / StageRegionData）
# ---------------------------------------------------------------------------
TABLE_STAGE_SECTION: Final[str] = "StageSectionData"
TABLE_STAGE_AREA: Final[str] = "StageAreaData"
TABLE_STAGE_REGION: Final[str] = "StageRegionData"
TABLE_STAGE_DISTRIBUTE: Final[str] = "StageDistributeData"


@dataclass(frozen=True)
class MainStageSection:
    """主线关卡配置（StageSectionData 的一行）。"""

    section_id: int
    section_name: str | None
    suggest_cp: int
    energy_cost: int
    cheat_check: int
    area_id: int
    area_name: str
    is_last_in_area: bool
    distribute_id: int
    next_section_id: int | None = None
    raw: TableRow | None = field(compare=False, repr=False, default=None)

    @property
    def is_cheat_check_required(self) -> bool:
        """是否要求真机存证校验（cheatCheck == 1）。"""
        return self.cheat_check == 1

    @property
    def stage_code(self) -> str:
        """解析关卡代号（第n章 x-y-z），避免直接向用户展示原始数字关卡 ID。"""
        import re

        z = ""
        if self.section_name:
            zm = re.search(r"\d+", self.section_name)
            if zm:
                z = zm.group(0)

        area = self.area_name.strip()
        # 第二部格式，例如 "2-1-1 「通道」" 或 "2-4-2"
        m2 = re.match(r"^2-(\d+)-(\d+)(?:\s+(.*))?$", area)
        if m2:
            chap, sub, _ = m2.groups()
            code = f"2-{chap}-{sub}-{z}" if z else f"2-{chap}-{sub}"
            return f"第二部第{chap}章 {code}"

        # 第一部格式，例如 "1-3 翡翠國的危機"（带「第一部」前缀，与第二部对齐）
        m1 = re.match(r"^(\d+)-(\d+)(?:\s+(.*))?$", area)
        if m1:
            chap, sub, _ = m1.groups()
            code = f"{chap}-{sub}-{z}" if z else f"{chap}-{sub}"
            return f"第一部第{chap}章 {code}"

        fallback = f"{self.area_name} {self.section_name or ''}".strip()
        return fallback or str(self.section_id)

    @property
    def friendly_name(self) -> str:
        """关卡友好全名（第n章 x-y-z 标题）。"""
        import re

        area = self.area_name.strip()
        title = ""
        m2 = re.match(r"^2-\d+-\d+(?:\s+(.*))?$", area)
        if m2 and m2.group(1):
            title = m2.group(1).strip()
        else:
            m1 = re.match(r"^\d+-\d+(?:\s+(.*))?$", area)
            if m1 and m1.group(1):
                title = m1.group(1).strip()

        if title:
            return f"{self.stage_code} {title}"
        return self.stage_code

    def describe(self) -> str:
        tag = "[敏感-需真机]" if self.is_cheat_check_required else "[常规]"
        return f"{self.friendly_name} {tag} CP:{self.suggest_cp} 体力:{self.energy_cost}"


@dataclass(frozen=True)
class MainStageArea:
    """主线小章节配置（StageAreaData 的一行）。"""

    area_id: int
    area_name: str
    star_count1: int
    star_count2: int
    star_count3: int
    section_ids: tuple[int, ...]
    raw: TableRow | None = field(compare=False, repr=False, default=None)


def load_main_stage_index(
    section_path: Path | None = None,
    area_path: Path | None = None,
) -> tuple[dict[int, MainStageSection], dict[int, MainStageArea], list[int]]:
    """读取主线关卡表与小章节表，构建全局关卡索引。

    :return: (section_dict, area_dict, ordered_section_ids)
    """
    areas_raw = read_table(area_path or table_path(TABLE_STAGE_AREA))
    sections_raw = read_table(section_path or table_path(TABLE_STAGE_SECTION))

    sections: list[MainStageSection] = []
    areas: dict[int, MainStageArea] = {}
    sec_idx = 0

    for a in areas_raw:
        area_id = to_int(a, "areaID")
        if area_id is None:
            continue
        area_name = to_text(a, "areaName") or ""
        star1 = to_int(a, "starCount1") or 0
        star2 = to_int(a, "starCount2") or 0
        star3 = to_int(a, "starCount3") or 0
        cnt = star3 // 3
        if cnt <= 0:
            continue

        if sec_idx + cnt <= len(sections_raw):
            secs_slice = sections_raw[sec_idx : sec_idx + cnt]
            area_sec_ids: list[int] = []
            for j, s in enumerate(secs_slice):
                sec_id = to_int(s, "sectionID")
                if sec_id is None:
                    continue
                area_sec_ids.append(sec_id)
                sec_name = to_text(s, "sectionName")
                cp = to_int(s, "suggestCP") or 0
                energy = to_int(s, "energyCost") or 6
                cheat = to_int(s, "cheatCheck") or 0
                dist = to_int(s, "stageDistributeID") or 0
                is_last = (j == cnt - 1)
                sections.append(
                    MainStageSection(
                        section_id=sec_id,
                        section_name=sec_name,
                        suggest_cp=cp,
                        energy_cost=energy,
                        cheat_check=cheat,
                        area_id=area_id,
                        area_name=area_name,
                        is_last_in_area=is_last,
                        distribute_id=dist,
                        raw=s,
                    )
                )
            areas[area_id] = MainStageArea(
                area_id=area_id,
                area_name=area_name,
                star_count1=star1,
                star_count2=star2,
                star_count3=star3,
                section_ids=tuple(area_sec_ids),
                raw=a,
            )
            sec_idx += cnt
        else:
            break

    sec_map: dict[int, MainStageSection] = {}
    ordered_ids: list[int] = []
    for i, sec in enumerate(sections):
        next_id = sections[i + 1].section_id if i + 1 < len(sections) else None
        updated = MainStageSection(
            section_id=sec.section_id,
            section_name=sec.section_name,
            suggest_cp=sec.suggest_cp,
            energy_cost=sec.energy_cost,
            cheat_check=sec.cheat_check,
            area_id=sec.area_id,
            area_name=sec.area_name,
            is_last_in_area=sec.is_last_in_area,
            distribute_id=sec.distribute_id,
            next_section_id=next_id,
            raw=sec.raw,
        )
        sec_map[sec.section_id] = updated
        ordered_ids.append(sec.section_id)

    return sec_map, areas, ordered_ids

