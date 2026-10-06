"""运行视图文案的「禁裸标识符」守卫（2026-10-04 新增）。

【为什么不能按"数字位数"判定】

一个很自然的写法是"运行视图里不许出现 8 位以上的数字"。**这条规则是错的**：
金币是本项目里唯一可能上亿的资源 —— ``金币×123456789`` 是一个完全合法、
而且必须被展示出来的数量。按位数判会把正常文案判成违规，逼着人去加白名单，
最后规则形同虚设。

【正确的判据是"语义"，不是"大小"】

要防的是「把**标识符**当成数量或名字来描述」，也就是两类真实出现过的错误：

    ``道具 200000050×500``      → itemID 被当成**名字**念出来
    ``获得 200000050 体力``     → itemID 被当成**数量**念出来

所以判据是：**已知标识符的值**出现在文案里。

【唯一的例外】

``×200000050`` 这种"数量恰好等于某个 ID"的极端巧合必须放行 ——
玩家真的攒到 200000050 金币时，文案本来就无法自证那是数量还是编号，
强行判违规只会制造噪音。判据因此收窄为：

    **标识符不是紧跟 ``×`` 出现** 即视为违规。

这样上面两个真实错误形态都能被抓到（一个在 ``×`` 前、一个压根没有 ``×``），
而合法的"数量恰好等于 ID"不会被误伤。
"""

from __future__ import annotations

import re
from typing import Final

#: 项目内会出现在响应里、且**不应**进入用户可见文案的标识符。
#:
#: 分三类，来源都写在注释里 —— 新增一类时请一并补上来源，
#: 否则下一个人无法判断某个数字该不该在表里。
KNOWN_IDENTIFIERS: Final[frozenset[int]] = frozenset(
    {
        # —— 道具 ID（models/const_ids.py 的 RPS_Const）——
        200000001,  # 钻石
        200000002,  # 体力
        200000016,
        200000017,
        200000030,  # 工会币
        200000050,  # 金币
        # —— 市集四件（models/market.py::MARKET_ITEMS）——
        200200000,  # 血钻
        200200003,  # 琥珀
        200600001,  # 爱心巧克力
        200600002,  # 幸福香氛
        # —— 商铺号 / 商品流水号（models/market.py）——
        398000015,  # 一般市集 storeType
        399000101,  # 货架商品 productSID 样例
    }
)

#: 把文案切成数字串。
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+")


def bare_identifiers(
    text: str, *, identifiers: frozenset[int] = KNOWN_IDENTIFIERS
) -> list[int]:
    """找出文案里"被当成名字或数量描述"的标识符。

    :param text: 待检查的用户可见文案。
    :param identifiers: 标识符集合；默认 :data:`KNOWN_IDENTIFIERS`。
    :return: 违规出现的标识符（按出现顺序，可能重复）；空列表 = 干净。
    """
    hits: list[int] = []
    for match in _NUMBER_RE.finditer(text):
        value = int(match.group())
        if value not in identifiers:
            continue
        # 紧跟 "×" 说明它是**数量**，不是标识符 —— 见模块文档的例外说明。
        if text[max(0, match.start() - 1) : match.start()] == "×":
            continue
        hits.append(value)
    return hits


def assert_no_bare_identifiers(
    text: str, *, identifiers: frozenset[int] = KNOWN_IDENTIFIERS
) -> None:
    """断言文案里没有把标识符当名字/数量（失败时给出可直接定位的信息）。

    :raises AssertionError: 文案里出现了裸标识符。
    """
    hits = bare_identifiers(text, identifiers=identifiers)
    assert not hits, (
        f"运行视图文案里出现了裸标识符 {hits}：{text!r}\n"
        "  说明有代码把 itemID / productSID / currencyType 当成了名字或数量。\n"
        "  修法：道具一律翻中文名（models.reward.item_name），"
        "数量一律走 reward_phrase/cost_phrase 并显式声明 QuantityScope。"
    )
