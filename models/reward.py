"""奖励 / 数量的统一呈现层（口径 + 中文名 + 量词）。

【为什么必须单独有这一层】

服务端把「拿到了什么」放在很多响应里（``6006`` / ``11010`` / ``37020`` /
``14004`` / ``24006`` / ``21017`` / ``11018`` …），但**同一个 ``itemAmount``
在不同接口里语义并不相同**：

======================  ==========================  =================================
响应                     数量语义                     依据
======================  ==========================  =================================
6006  DMRewardRes        **领取后持有量**             Q-021（真机）+ recon 21.13
11010 SweepSectionRes    **扫荡后持有量**             Q-021（真机结案）
37020 GetFreeGiftPacket  **领取后持有量**             Q-021 同源
14004 / 24006 / 21017 /
11018                     **尚未实证**                 见 notes/open_questions.md
======================  ==========================  =================================

以前全项目只有一条模板 ``f"{itemID}×{itemAmount}"``，无差别套在上面这些响应上 ——
于是「把持有量当获得量」「把物品 ID 当数量」「把组数当件数」这三类错误反复复发。
根因不是某处写错，而是**"口径"这个概念在代码里不存在**。

本模块把口径提升为类型（:class:`QuantityScope`），并立一条硬规则：

    ``UNKNOWN`` 口径**一律不展示数字** —— 要展示数量，先拿出实证。

【和 ``models/`` 其它模块的分工】
本模块是**纯呈现层**：不联网、不解析字节、不依赖 ``tasks/``（分层方向是
``tasks/`` → ``models/``，反过来会形成循环导入）。它只回答一个问题：
"拿到一堆 GetObjClass，在用户可见文案里该怎么写才不算撒谎"。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import Enum
from typing import Any, Final

from models.const_ids import CONSTS

# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------
class QuantityScope(str, Enum):
    """``itemAmount`` 的语义口径。

    :attr:`GAINED` 与 :attr:`HELD` **都必须有实证**（抓包或真机观察）才能使用；
    拿不准就用 :attr:`UNKNOWN` —— 它不会让文案变得"信息少一点"，
    而是把"我们其实不知道"如实说出来。
    """

    #: 本次增量：服务端明确回传 delta
    GAINED = "gained"
    #: 操作后的持有量（Q-021：6006 / 11010 / 37020 均属此类）
    HELD = "held"
    #: 口径未实证 —— **禁止展示数字**，也禁止使用"获得/消耗"这类带方向的词去描述数量
    UNKNOWN = "unknown"


#: 已知道具 ID → 中文名。
#:
#: 不在这里的道具**不进运行视图**（裸数字对最终用户毫无意义），
#: 但会算进 :func:`count_items` 的件数 —— 这样"拿了 3 件不知道是什么的东西"
#: 仍然能被如实说出来。
_ITEM_NAMES: Final[dict[int, str]] = {
    int(CONSTS["DIAMOND_ID"]): "钻石",
    int(CONSTS["COINS_ID"]): "金币",
    int(CONSTS["ENERGY_ID"]): "体力",
    int(CONSTS["SWEEPTICKET_ID"]): "扫荡券",
    int(CONSTS["Alliance_Currency_ID"]): "工会币",
    # ★ 2026-10-06：登入奖励的常驻道具（名字照抄游戏配置表 ItemData；
    #   证据链见 notes/recon_findings.md 的「登录签到」节）。
    #   只收"跨活动稳定复用"的道具ID，一次性活动奖励仍走件数降级文案。
    200707001: "一般寶石袋",
    200707002: "高級寶石袋",
    200600003: "魅惑之藥",
}


def item_name(item_id: int) -> str | None:
    """道具 ID 的中文名；不认识的返回 ``None``。"""
    return _ITEM_NAMES.get(int(item_id))


# ---------------------------------------------------------------------------
# 摊平与计数（量词口径）
# ---------------------------------------------------------------------------
def iter_items(groups: Iterable[Any] | Any | None) -> Iterator[Any]:
    """把奖励明细摊平成"一行一件道具"。

    :param groups: ``GetObjClass`` 的可迭代对象（每组含 ``itemList``）。

    【为什么同时接受"已经摊平过的 ``ItemClass`` 列表"】
    有的模型直接给的是 ``list[ItemClass]``（例如 ``11010`` 的
    :meth:`models.activity_stage.SweepSectionRes.settlement_items`），
    而有的给的是 ``list[GetObjClass]``。只认一种的话，传错层级会**静默返回 0 件**
    —— 那是最难查的一类 bug。所以这里按元素形状判断：

    * 元素有 ``itemList`` → 当作一组，继续往里摊平；
    * 元素只有 ``itemID``（= 已经是一件道具）→ 直接产出。

    ``None``、缺字段的元素、以及服务端未建模的 ``bytes`` 占位都安全跳过。
    """
    if groups is None:
        return
    for group in groups:
        inner = getattr(group, "itemList", None)
        if inner is None:
            if hasattr(group, "itemID"):
                yield group
            continue
        for item in inner:
            yield item


def count_items(groups: Iterable[Any] | Any | None) -> int:
    """**件数** = 摊平后的道具条目数（``GetObjClass`` 组数 ≠ 件数）。

    .. note::
       这是本项目唯一被允许称为"件"的口径。``len(groups)`` 是**组数**，
       两者的差别正是 2026-10-04 修掉的一处错误描述（``top_pvp_box``）。
    """
    return sum(1 for _ in iter_items(groups))


def count_item_kinds(groups: Iterable[Any] | Any | None) -> int:
    """**种数** = 去重后的 ``itemID`` 个数（与 :func:`count_items` 的"件数"区分）。"""
    return len({int(item.itemID) for item in iter_items(groups)})


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------
def reward_text(groups: Iterable[Any] | Any | None, *, scope: QuantityScope) -> str:
    """把若干 ``GetObjClass`` 汇成「金币×1000、钻石×60」。

    :param scope: 数量口径。**必填** —— 强制每个调用点显式表态，
        而不是默认继承一个可能是错的假设。
    :return: 只保留认识名字的道具；``scope=UNKNOWN`` 时**返回空串**
        （口径未实证的数量不进用户可见文案）；一个都不认识时也返回空串。

    .. note::
       ``UNKNOWN`` 返回空串是刻意的：调用方若仍想让用户知道"拿到了东西"，
       应当改用 :func:`reward_phrase`（它会退化成"获得 N 件道具"）。
    """
    if scope is QuantityScope.UNKNOWN:
        return ""
    parts = [
        f"{name}×{item.itemAmount}"
        for item in iter_items(groups)
        if (name := item_name(item.itemID))
    ]
    return "、".join(parts)


def _phrase(
    groups: Iterable[Any] | Any | None,
    *,
    scope: QuantityScope,
    gained_verb: str,
    held_verb: str,
) -> str:
    """带方向词的一行（:func:`reward_phrase` / :func:`cost_phrase` 的共用实现）。

    方向词与口径**一一绑定**：``GAINED`` 才配"获得/消耗"，
    ``HELD`` 只能配"当前持有/剩余"。这条绑定的意义在于 ——
    光看文案就能知道数字是哪种口径，不必再去翻协议。
    """
    if scope is QuantityScope.UNKNOWN:
        count = count_items(groups)
        return f"{gained_verb} {count} 件道具" if count else ""
    text = reward_text(groups, scope=scope)
    if not text:
        return ""
    verb = gained_verb if scope is QuantityScope.GAINED else held_verb
    return f"{verb}：{text}"


def reward_phrase(groups: Iterable[Any] | Any | None, *, scope: QuantityScope) -> str:
    """「获得了什么」的一行文案。

    :return: 三种形态之一，没有任何道具时返回空串。

        * ``GAINED``  → ``获得：金币×1000``
        * ``HELD``    → ``当前持有：金币×3661万``
        * ``UNKNOWN`` → ``获得 3 件道具``（**不报数量**）
    """
    return _phrase(
        groups, scope=scope, gained_verb="获得", held_verb="当前持有"
    )


def cost_phrase(groups: Iterable[Any] | Any | None, *, scope: QuantityScope) -> str:
    """「花掉了什么」的一行文案（与 :func:`reward_phrase` 同构）。

    :return: ``GAINED`` → ``消耗：金币×5000``；``HELD`` → ``剩余：金币×…``；
        ``UNKNOWN`` → ``消耗 1 件道具``；没有明细时返回空串。
    """
    return _phrase(groups, scope=scope, gained_verb="消耗", held_verb="剩余")
