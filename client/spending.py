"""消费许可（SpendPolicy）：**所有会花掉玩家资源的动作，必须先过这道闸门**。

【这个模块存在的唯一理由】
游戏里的"领取"大多是只进不出的，但有几类动作会**真的花掉钻石**，例如
宴席系统里"花钻石邀请好友同桌吃饭"。一旦自动化流程在你不注意的时候
把钻石花掉，损失是不可逆的 —— 所以本模块把这件事做成一个**默认关闭**的开关：

    allow_diamond = False  →  任何花钻石的请求**连字节都不会被组装出来**

【为什么叫 fail-closed（失败即关闭）】
默认值取 ``False``，而不是"未配置就放行"。这类设计的判断标准很简单：
猜错两种方向的代价并不对称 ——
"本来允许却被拦住"只是少领一次奖励；"本来禁止却放行"是真实扣钻石。
代价不对称时，默认值必须站在保守一侧。

【为什么放在 client 层而不是 tasks 层】
将来使用它的地方不止日常任务：商店购买、抽卡、宴席邀请……都走同一把闸门。
放在底层意味着任何一层都能 ``from client.spending import SpendPolicy``，
不必为了一个开关去 import 任务层代码（那会造成循环依赖）。

【三层防线是怎么配合的（初学者最容易漏掉的一层）】
1. 本模块 → 全局策略（开关从哪来、允不允许、**按用途**放行、单次/累计上限）；
2. ``tasks/base.py`` 的 ``BaseTask.spends_diamond``（整任务都会花钱）与
   ``diamond_purposes``（只有某个分支会花钱）+ ``check_preconditions()``
   → **类级声明**：前者直接把任务拦死，后者只提示"哪个分支会被跳过"，
   两者都是"哪怕任务作者在某个 if 分支里忘了判断也拦得住"的那一道；
3. ``BaseTask.request_diamond_spend(..., purpose=...)`` → **调用点级**：在真正组包
   之前再问一次，并带上金额、用途与理由，日志里能追溯到"这一笔为什么花"。

三道都指向同一个 :class:`SpendPolicy` 实例，所以将来设置界面只需要构造它一次。

【为什么要"用途"这一维（2026-09-21 新增）】
宴席被拆成"①邀请好友=花钱 / ②确认吃=免费"两步之后，"允许花钻石"就不再等于
"什么都能花"：用户可能只想在宴席请客上放行。于是总闸（``allow_diamond``）之外
再加一张用途白名单（``allowed_purposes``），**两者是与关系**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Final

import config
from client.exceptions import SpendBlockedError


class SpendKind(IntEnum):
    """消费的种类。

    .. note::
       目前只有 :attr:`DIAMOND` 是真实存在的路径（宴席邀请好友）。
       :attr:`ITEM` 为将来预留：一旦确认某个动作消耗具体道具（配置表里
       ``useItemID`` 非 ``NULL`` 的那种），就可以直接走同一条闸门，不必新增机制。
    """

    #: 消耗钻石（默认货币）。配置表 ``ExtraEnergyData.useItemID`` 为 ``NULL`` 时属这一类。
    DIAMOND = 0
    #: 消耗具体道具（预留）
    ITEM = 1


class SpendPurpose(IntEnum):
    """这笔钱**花在哪个功能上** —— 消费种类之外的第二个维度。

    【为什么在"种类"之外还要"用途"】
    宴席被拆成两步之后（① 邀请好友 = 花钻石；② 确认吃宴席 = 免费），
    "允许花钻石"就不再是一个全局布尔值该管的事：用户可能只想在**宴席请客**
    上放行，而不希望商店购买、抽卡这些将来的功能顺带也能花钱。
    于是把放行做成"按用途授权"：

    .. code-block:: python

        SpendPolicy(
            allow_diamond=True,                                   # 总闸
            allowed_purposes=frozenset({SpendPurpose.BBQ_INVITE}),  # 只有这一个用途
        )

    .. note::
       ``allowed_purposes`` 为**空集合**时表示"一个用途都不放行"（fail-closed）。
       这与 ``allowed_item_ids`` 的空集合语义一致：忘了配置 = 全禁，而不是全开。
       总闸与用途白名单是**与**关系，两道门都过才放行。
    """

    #: 宴席：花钻石邀请好友一起吃（``ExtraEnergyData`` 的高档位，最高档 31 钻）
    BBQ_INVITE = 0
    #: 购买体力：花钻石购买体力（消息号 24005 BuyEnergyPacket）
    BUY_ENERGY = 1


#: 用途 → "怎么打开这个开关"的提示（写进异常信息，省得用户去翻代码）。
#:
#: 放在这里的理由：新增用途时，只有三处需要改 —— 枚举、本表、``config`` 里的开关。
#: 异常信息因此永远能直接告诉用户该敲哪条命令，而不必让人去搜索。
_PURPOSE_HINTS: Final[dict[SpendPurpose, str]] = {
    SpendPurpose.BBQ_INVITE: (
        f"命令行：--invite-friends；环境变量：{config.BBQ_INVITE_FRIENDS_ENV_NAME}=1；"
        "本地配置：config_local.py 里 BBQ_INVITE_FRIENDS = True"
    ),
    SpendPurpose.BUY_ENERGY: (
        f"命令行：--buy-energy；环境变量：{config.BUY_ENERGY_ENV_NAME}=1；"
        "本地配置：config_local.py 里 BUY_ENERGY = True"
    ),
}


def purposes_from_config() -> frozenset[SpendPurpose]:
    """把 ``config`` 里的**用途级开关**汇总成一张白名单。

    :return: 已由配置授权的用途集合；一个都没开时是空集合（= 全禁）。

    【为什么要集中在这里，而不是各任务自己读环境变量】
    "哪些用途被授权"必须只有**一个**答案。散落到各任务里迟早会出现
    "任务 A 说开了、闸门说没开"这种自相矛盾的状态，而花钱这件事上
    任何矛盾都必须按最保守的一侧解释 —— 与其事后解释，不如从源头合成一处。
    """
    purposes: set[SpendPurpose] = set()
    if config.BBQ_INVITE_FRIENDS:
        purposes.add(SpendPurpose.BBQ_INVITE)
    if getattr(config, "BUY_ENERGY", False):
        purposes.add(SpendPurpose.BUY_ENERGY)
    return frozenset(purposes)


@dataclass(frozen=True)
class SpendPolicy:
    """消费许可策略（**不可变** —— 运行中被悄悄改掉是最危险的事）。

    :param allow_diamond: 是否允许花钻石。**默认 ``False``**（见模块文档的 fail-closed 说明）。
    :param allowed_purposes: 允许花钱的**用途**白名单（见 :class:`SpendPurpose`）；
        **空集合表示一个用途都不放行** —— 与总闸是"与"关系。
    :param allowed_item_ids: 允许消耗的道具 ID 白名单；**空集合表示不允许任何道具消费**。
    :param max_diamond_per_call: 单次调用允许的最大钻石数；``0`` 表示不限制单次。
    :param max_diamond_per_run: 一次任务流程内允许累计消耗的钻石数；``0`` 表示不限制累计。

    .. warning::
       ``max_diamond_per_call`` / ``max_diamond_per_run`` 只在
       :attr:`allow_diamond` 为 ``True`` 时才有意义 —— 开关关闭时它们不构成
       "限额放行"，而是完全不放行。**不要**把它们当成另一种开启方式。
    """

    #: 总开关：是否允许花钻石
    allow_diamond: bool = False
    #: 允许花钱的**用途**白名单（空 = 一个用途都不放行）
    #:
    #: 【为什么默认是空的，而不是"不限用途"】
    #: "不限用途"意味着"开了总闸就什么都能花"，而用户想表达的往往是
    #: "我这次只想请客，不想顺带买别的东西"。默认全禁 + 显式授权，
    #: 才符合"代价不对称时站在保守一侧"这条原则（少领一次 vs 真扣钻石）。
    allowed_purposes: frozenset[SpendPurpose] = field(default_factory=frozenset)
    #: 允许消耗的道具 ID 白名单（空 = 不允许）
    allowed_item_ids: frozenset[int] = field(default_factory=frozenset)
    #: 单次上限（0 = 不限制）
    max_diamond_per_call: int = 0
    #: 累计上限（0 = 不限制）
    max_diamond_per_run: int = 0
    #: 本次流程已累计花掉的钻石数。
    #:
    #: 为什么由策略对象自己记，而不是让任务各记各的？
    #: 累计上限必须是**全局**概念：宴席任务里两条付费档、商店购买里三条，
    #: 各自记各自的等于没有上限。放在不可变的策略对象上，
    #: 每次获批都生成新对象（见 :meth:`charge`），累计值就无法被某处悄悄清零。
    _spent_so_far: int = 0

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls) -> "SpendPolicy":
        """按 ``config`` 的当前配置构造策略（环境变量 / 本地配置都汇到这里）。

        这是**默认来源**：任务不显式注入 ``spend_policy`` 时用它。
        将来的设置界面只要自己构造好 :class:`SpendPolicy` 传进来即可，
        本函数一行都不用改。
        """
        return cls(
            allow_diamond=config.ALLOW_DIAMOND_SPEND,
            allowed_purposes=purposes_from_config(),
        )

    def allows_purpose(self, purpose: SpendPurpose) -> bool:
        """**询问**某个用途是否已被授权（不抛异常、不发请求）。

        :param purpose: 用途（例如 :attr:`SpendPurpose.BBQ_INVITE`）。
        :return: 总闸与用途白名单**都**放行时返回 ``True``。

        【与 :meth:`check` 的分工（最容易搞混的一对）】
        - :meth:`allows_purpose` 是**询问**：任务用它来决定"要不要走花钱那条分支"，
          未授权是**正常状态**（例如宴席只吃免费档），所以绝不抛异常；
        - :meth:`check` 是**放行凭证**：真正组包之前必须调用一次，未授权就
          抛 :class:`~client.exceptions.SpendBlockedError` —— 这才是"没授权就一定
          发不出花钱的包"的硬保证（因为请求字节压根不会被构造）。
        """
        return self.allow_diamond and purpose in self.allowed_purposes

    def _purposes_text(self) -> str:
        """已授权用途的可读文本（供日志与异常信息使用）。"""
        if not self.allowed_purposes:
            return "无（一个用途都没授权）"
        return "、".join(
            sorted(purpose.name.lower() for purpose in self.allowed_purposes)
        )

    @property
    def spent_so_far(self) -> int:
        """本次流程已累计花掉的钻石数（供日志与结果展示）。"""
        return self._spent_so_far

    # ------------------------------------------------------------------
    # 裁决
    # ------------------------------------------------------------------
    def check(
        self,
        *,
        kind: SpendKind,
        amount: int,
        reason: str,
        purpose: SpendPurpose | None = None,
    ) -> "SpendPolicy":
        """检查「这次消费」是否被允许；允许时返回**已登记该笔消费**的新策略。

        :param kind: 消费种类。
        :param amount: 本次将消耗的数量（钻石数 / 道具个数）。
        :param reason: 人类可读的理由（会写进异常信息与日志，便于事后审计）。
        :param purpose: 这笔钱花在哪个功能上（见 :class:`SpendPurpose`）。
            ``None`` 表示"不区分用途"（旧行为，只受总闸管辖）；
            给了用途就必须**同时**通过用途白名单。
        :return: 记账后的新策略对象（累计值 + ``amount``）。
        :raises client.exceptions.SpendBlockedError: 不被允许时。

        .. note::
           **本方法不发送任何请求**，它只在组包之前做判断。
           所以开关关闭时，请求字节永远不会被组装出来 ——
           这就是"关闭则一定不能使用钻石"在代码里的确切含义。

        .. note::
           返回值必须被调用方接住（``self.spend_policy = self.spend_policy.check(...)``），
           否则累计上限会失效。``BaseTask.request_diamond_spend()`` 已经替你接好了，
           任务代码里请优先用那个方法，而不是直接调本方法。
        """
        # ① 总开关：钻石路径在开关关闭时一律拒绝，与金额大小无关。
        if kind is SpendKind.DIAMOND and not self.allow_diamond:
            raise SpendBlockedError(
                f"已阻止一笔钻石消费（{amount} 钻）：{reason}\n"
                "  原因：钻石使用开关处于【关闭】状态（默认值就是关闭）。\n"
                "  开启方式（任选其一）：\n"
                "    · 命令行：python main.py run <任务> --allow-diamond\n"
                f"    · 环境变量：{config.ALLOW_DIAMOND_SPEND_ENV_NAME}=1\n"
                "    · 本地配置：在 config_local.py 里写 ALLOW_DIAMOND_SPEND = True"
            )

        # ② 用途白名单：总闸开了**不等于**什么都能花。
        #    例如用户只想在"宴席请客"上放行，就不该顺带把将来的商店购买也放开。
        #    purpose=None（旧调用）这一步不参与判断，以保持既有任务的行为不变。
        if (
            kind is SpendKind.DIAMOND
            and purpose is not None
            and purpose not in self.allowed_purposes
        ):
            raise SpendBlockedError(
                f"已阻止一笔钻石消费（{amount} 钻）：{reason}\n"
                f"  原因：用途「{purpose.name}」未被授权。\n"
                f"  当前已授权用途：{self._purposes_text()}。\n"
                f"  开启方式：{_PURPOSE_HINTS.get(purpose, '（该用途暂无独立开关）')}\n"
                "  ⚠ 总闸（--allow-diamond）也必须同时打开 —— 两道门是与关系。"
            )

        # ③ 道具消费：本阶段没有开放任何道具白名单。
        #    这里刻意把"空集合"解释成"一个都不允许"，而不是"不限"——
        #    后者会让"忘了配置"变成"全部放行"，正好是这类事故的典型成因。
        if kind is SpendKind.ITEM:
            raise SpendBlockedError(
                f"已阻止一笔道具消费（{amount} 个）：{reason}\n"
                "  原因：道具消费白名单为空（allowed_item_ids 未配置）。\n"
                "  说明：本项目当前只放行钻石路径，道具消费属于预留能力，"
                "确认协议后再按需开放。"
            )

        # ④ 限额（只有开关已开启时才走得到这里）。
        if self.max_diamond_per_call and amount > self.max_diamond_per_call:
            raise SpendBlockedError(
                f"已阻止一笔钻石消费（{amount} 钻）：{reason}\n"
                f"  原因：超过单次上限 {self.max_diamond_per_call} 钻。"
            )
        if self.max_diamond_per_run and self._spent_so_far + amount > self.max_diamond_per_run:
            raise SpendBlockedError(
                f"已阻止一笔钻石消费（{amount} 钻）：{reason}\n"
                f"  原因：本次流程累计将达 {self._spent_so_far + amount} 钻，"
                f"超过累计上限 {self.max_diamond_per_run} 钻。"
            )

        return self.charge(kind=kind, amount=amount)

    def charge(self, *, kind: SpendKind, amount: int) -> "SpendPolicy":
        """登记一笔**已获准**的消费，返回累加后的新策略对象。

        为什么不让调用方自己 ``+=``？
        因为本类是 ``frozen`` 的（防误改），累加只能通过生成新对象来表达 ——
        这正好留下一道清晰的审计线索：策略对象的流转过程就是消费流水。
        """
        if kind is not SpendKind.DIAMOND:
            return self
        return SpendPolicy(
            allow_diamond=self.allow_diamond,
            allowed_purposes=self.allowed_purposes,
            allowed_item_ids=self.allowed_item_ids,
            max_diamond_per_call=self.max_diamond_per_call,
            max_diamond_per_run=self.max_diamond_per_run,
            _spent_so_far=self._spent_so_far + amount,
        )

    def refund(self, amount: int) -> "SpendPolicy":
        """冲回一笔**实际没有发生**的消费，返回递减后的新策略对象。

        【为什么需要它（2026-10-04 宴席实测）】
        任务的正确顺序是"先过闸门（预扣）→ 再发包"，但服务端仍可能拒绝
        （例如宴席邀请好友数超限回 ``errorCode=-6``）—— 预扣的钻石并没有
        真花出去，消耗统计必须冲回，否则运行汇总里的"共消耗 N 钻"是假的。
        与 :meth:`charge` 一样以新对象表达，保留审计流水。
        """
        refunded = max(self._spent_so_far - amount, 0)
        return SpendPolicy(
            allow_diamond=self.allow_diamond,
            allowed_purposes=self.allowed_purposes,
            allowed_item_ids=self.allowed_item_ids,
            max_diamond_per_call=self.max_diamond_per_call,
            max_diamond_per_run=self.max_diamond_per_run,
            _spent_so_far=refunded,
        )

    def describe(self) -> str:
        """一行摘要（供启动日志与演练输出打印）。"""
        if not self.allow_diamond:
            return "钻石使用=禁止（开关关闭）"
        detail: list[str] = ["钻石使用=允许"]
        # 用途单独列出来：光看"允许"很容易误以为"什么都能花"，而实际未必。
        detail.append(f"用途={self._purposes_text()}")
        if self.max_diamond_per_call:
            detail.append(f"单次≤{self.max_diamond_per_call}")
        if self.max_diamond_per_run:
            detail.append(f"累计≤{self.max_diamond_per_run}")
        return "，".join(detail)


#: 最保守的策略：什么都不允许（等价于默认值，供"必须明确禁止"的场景使用）。
DENY_ALL: Final[SpendPolicy] = SpendPolicy()

