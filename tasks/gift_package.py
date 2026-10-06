"""特惠礼包里的每日免费领取（消息号 37001 → 37002 / 37019 → 37020）。

【这个任务要解决的事】
首页「特惠礼包」面板里有两样是**每日免费**的（例如免费钻石、免费扫荡券），
玩家点一下就白拿 —— 本任务就是替你把它们点掉：

    37001 GetGiftListPacket   拉整页礼包列表
    37019 GetFreeGiftPacket   对其中每个「免费」礼包发一次领取

【怎么认出"那是钻石/扫荡券"】
**不看名字，只看领到了什么**：响应 ``GetFreeGiftPacketRes.gotList`` 里带的是
道具 ID，日志里用 ``models/const_ids.py`` 把它们翻译成人话：

    200000001 → 钻石（DIAMOND_ID）
    200000016 → 扫荡券（SWEEPTICKET_ID）

这样将来活动改成送别的东西，本任务一行都不用改 —— 名字会变，ID 不会。

【安全边界：本任务**完全不花钱**】
候选清单的来源是 ``GiftInfoClass.isFree != 0``，只领免费礼包；
付费礼包走的是另一条链路（37003 ``PurchaseGiftPacket``，涉及真实支付渠道），
本项目**不实现**。所以本任务 ``spends_diamond`` 保持默认的 ``False``：
它不需要、也不应该依赖钻石开关。

【⚠️ 尚未实测确认】
- ``GetGiftListPacket`` 的 ``cashType`` / ``channelType`` 取值（先传 0）；
- ``buyCount`` / ``buyLimit`` 的确切语义（先按"buyLimit<=0 表示不限"处理，
  并且**即使预判为已领也会照样尝试**——见 :meth:`_claimable` 的说明）。
"""

from __future__ import annotations

from typing import Any

from client.game_client import GameClient
from models.gift_package import (
    GetFreeGiftPacket,
    GetFreeGiftPacketRes,
    GetGiftListPacket,
    GetGiftListRes,
)
from tasks.base import BaseTask, QuantityScope, TaskResult, register_task, reward_phrase


@register_task
class FreeGiftTask(BaseTask):
    """领取特惠礼包中的每日免费礼包（含免费钻石 / 免费扫荡券）。"""

    name = "free_gift"

    # ------------------------------------------------------------------
    # ① 拉列表
    # ------------------------------------------------------------------
    def _fetch_gifts(self, game: GameClient) -> GetGiftListRes:
        """拉取礼包列表（消息号 37001 → 37002）。

        【★ 2026-10-03 参数修订】筛选参数改用 10.3 安卓抓包实测值
        （``config.GIFT_STORE_*``：platform=4 / cashType=7 / channelType=2）。
        旧的 ``{platform=2, cashType=0, channelType=0}`` 服务端只回**空列表**，
        免费钻石/扫荡券一个都挑不出来 —— 本任务"什么都没领"的直接原因。
        """
        import config

        packet = GetGiftListPacket.for_normal_store(
            platform=config.GIFT_STORE_PLATFORM,
            cash_type=config.GIFT_STORE_CASH_TYPE,
            channel_type=config.GIFT_STORE_CHANNEL_TYPE,
        )
        view = game.send(packet, expect=GetGiftListRes, include_defaults=True)
        return view.decode_sub_packet(GetGiftListRes)

    # ------------------------------------------------------------------
    # ② 挑候选
    # ------------------------------------------------------------------
    def _claimable(self, res: GetGiftListRes) -> list[int]:
        """挑出这次要尝试领取的礼包 ID。

        .. note::
           **不只看 ``can_claim()``**：``can_claim()` 里的 ``buyLimit`` 判断
           依赖尚未实测确认的语义。如果只信它，一旦理解反了就会**整体漏领**
           （而且静默）。所以这里的策略是：
           免费礼包**全部尝试一次**，让服务端去做最终裁决 ——
           重复领取最多回一个错误码，代价远小于漏领。

           ``can_claim()` 的预判只用于…打印提示（告诉你有几个可能已达上限）。
        """
        return [gift.giftID for gift in res.free_gifts()]

    # <<< APPEND MARKER >>>
    # ------------------------------------------------------------------
    # ③ 领取单个免费礼包
    # ------------------------------------------------------------------
    def _claim_raw(
        self, game: GameClient, gift_id: int
    ) -> "GetFreeGiftPacketRes | None":
        """发一次领取请求并返回**原始响应**（``None`` = 没拿到响应）。

        之所以要有这一层：月卡类礼包要合并多行奖励成一行的汇总，
        而原有的 :meth:`_claim` 会把结果压成一句含 errorCode 的话，
        汇总时就看不见具体拿到什么了。
        """
        packet = GetFreeGiftPacket.for_gift(gift_id)
        view = game.send(packet, expect=GetFreeGiftPacketRes, include_defaults=True)
        return view.decode_sub_packet(GetFreeGiftPacketRes)

    def _claim(self, game: GameClient, gift_id: int) -> tuple[bool, str]:
        """领取一个免费礼包（消息号 37019 → 37020）。

        :return: ``(是否成功, 可读结果)``。

        .. warning::
           **不重试**。免费礼包是"每日/每期限领一次"的，
           超时后服务端可能已经发过奖；重试只会拿到"已达上限"，
           既没意义又可能触发风控。每天重跑一次本任务才是正确的复查方式。

        .. note::
           **失败文案不再进运行视图**（2026-10-05 用户需求，见 :meth:`execute`）。
           这里的 errorCode 只写给 DEBUG，供排查时对照协议表；
           界面上不会出现裸 errorCode。
        """
        res = self._claim_raw(game, gift_id)
        if res is None or not res.is_success():
            code = getattr(res, "errorCode", "?")
            return False, f"errorCode={code}"
        return True, self._reward_text(res)

    @staticmethod
    def _reward_text(res: GetFreeGiftPacketRes) -> str:
        """把奖励汇总成"**当前持有**"口径的一行（只保留认识名字的道具）。

        【★ 2026-10-03 措辞修正】``37020 gotList`` 的数量是服务端回传的
        **领取后钱包余额**，不是本次入账增量（与 11010/6006 同一语义，
        见 Q-021）—— 按"获得"展示会让人误以为领到了账户级的数字。

        【★ 2026-10-04】口径升级为类型参数（``QuantityScope.HELD``），
        由 ``models/reward.py`` 统一选词。
        """
        phrase = reward_phrase(res.gotList, scope=QuantityScope.HELD)
        return f"已领取（{phrase}）" if phrase else "已领取（服务端未回道具明细）"

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """拉列表 → 逐个领免费礼包 → 汇总。**全程不花钱。**

        :raises client.exceptions.CherrytaleError: 网络失败时（由基类转成失败结果）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            res = self._fetch_gifts(game)
            free_gifts = res.free_gifts()
            monthly = [gift for gift in free_gifts if self._is_monthly(gift)]
            monthly_ids = {gift.giftID for gift in monthly}
            plain = [gift for gift in free_gifts if gift.giftID not in monthly_ids]
            self.logger.info(
                "礼包共 %d 个，其中免费 %d 个（含月卡类 %d 个）",
                len(res.giftList),
                len(free_gifts),
                len(monthly),
            )
            # 逐个礼包的 giftID / 领取进度属于**开发者日志**内容
            # （运行视图不出现裸 giftID —— 见「结果视图纪律」）。
            self.logger.debug(
                "免费礼包清单：%s",
                "；".join(gift.summary() for gift in plain) or "无",
            )

            claimed: list[str] = []
            failed_count = 0
            for gift in plain:
                ok, text = self._claim(game, gift.giftID)
                # 运行视图里用礼包名（不带 giftID 裸数字，2026-10-03 精简原则）
                label = f"礼包「{gift.name}」" if gift.name else "礼包"
                line = f"{label}：{text}" if text else label
                if ok:
                    claimed.append(line)
                    self.logger.info("✔ 已领取 %s", line)
                else:
                    # ★ 2026-10-05 用户需求：**失败不出现在运行日志里**。
                    # 免费礼包的"失败"几乎全是"今日已领 / 需要花钱开通 /
                    # 活动时间已过"——都不是工具本身的问题，只占篇幅。
                    # 这与本文件 `_claim_monthly()` 的口径完全一致：
                    # 详情留给 DEBUG，运行视图只保留真正做成的事。
                    failed_count += 1
                    self.logger.debug(
                        "礼包「%s」未领取：%s（通常是今日已领或活动已结束）",
                        gift.name or "<无名>",
                        text,
                    )

            monthly_line = self._claim_monthly(game, monthly)

        # 月卡那条**永远排在最后**：它是"有没有买月卡"的事实陈述，
        # 不是这次领取成败的主线（即便领不到也不算本任务失败）。
        if monthly_line:
            claimed.append(monthly_line)

        if not free_gifts:
            message = (
                "礼包列表里没有免费礼包 —— 若今天本该有，"
                "请核对 GetGiftListPacket 的 giftType/cashType/channelType 参数取值"
            )
        elif not claimed and not monthly_line:
            # ★ 2026-10-05：免费礼包一个都没领到，且没有月卡类可陈述。
            # 这是**正常状态**（今天已经领过了），不是失败 —— 如实说明即可，
            # 不再罗列逐个礼包的失败原因（那正是用户要求删掉的噪音）。
            message = "今日免费礼包均已领取（或活动尚未开放）"
        elif not claimed:
            # 只有月卡那一行有内容 → 直接陈述它，不写"领到 0 个"
            message = monthly_line
        else:
            message = f"免费礼包全部领取成功（{len(claimed)} 个）：" + "；".join(claimed)

        if monthly_line and claimed:
            message += f"；{monthly_line}"

        return TaskResult(
            task=self.name,
            # ★ 2026-10-05：**流程跑完即为成功**。"今日已领"不是失败，
            # 旧写法 `not failed or bool(claimed)` 会把"全都已领"判成失败（✘），
            # 让运行结果里出现一个红色的、其实什么都正常的条目。
            ok=True,
            message=message,
            data={
                "gift_total": len(res.giftList),
                "free_total": len(free_gifts),
                # 只留计数：逐条展示文本已由 message 承载，data 不再重复一份
                # （2026-10-04「data 一并收敛」）
                "claimed_count": len(claimed),
                # 失败数仍然保留 —— 它是**开发者日志/导出**里有用的排查线索，
                # 只是不再进运行视图（见上面循环里的 debug 分支）。
                "failed_count": failed_count,
                "monthly_total": len(monthly),
            },
        )

    # ------------------------------------------------------------------
    # ④-b 月卡类礼包：照领，但只合并输出一行
    # ------------------------------------------------------------------
    @staticmethod
    def _is_monthly(gift: Any) -> bool:
        """是否月卡类礼包（按名字关键字判，见 :data:`config.GIFT_MONTHLY_KEYWORDS`）。

        【为什么不删掉它】它是 `isFree != 0` 的（服务端就是这么标的），
        按原 giftID 照发一次请求即可 —— 买了月卡的号能真的领到；
        没买的号只是拿回一个错误码，代价一次往返。
        """
        import config

        name = str(getattr(gift, "name", "") or "")
        return any(keyword in name for keyword in config.GIFT_MONTHLY_KEYWORDS)

    def _claim_monthly(self, game: GameClient, gifts: list[Any]) -> str:
        """逐个尝试领取月卡类礼包，**汇总成一行**。

        :return: 一行结果；没有任何月卡类礼包时返回空串（调用方据此省略）。

        【为什么合并（2026-10-04 用户需求）】
        服务端把它拆成了 3 条（不同档位/天数），但用户眼里只有一件事：
        "月卡礼包今天领没领到"。3 行结果里 2 行是噪音，还会让人误判任务失败。

        【为什么失败不打错误码】
        errorCode 在这里只能表达同一个事实：**还没买月卡**。
        把它写成人为的话即可；保留裸数字只会让人去翻协议表却查不出更多信息。
        """
        import config

        if not gifts:
            return ""

        got: list[Any] = []
        ok_count = 0
        for gift in gifts:
            res = self._claim_raw(game, gift.giftID)
            if res is None or not res.is_success():
                # 失败不记录任何 errorCode（见 docstring），也不写 WARNING：
                # WARNING 会进运行结果视图，那正是用户不希望看到的东西。
                self.logger.debug(
                    "月卡类礼包 #%s 领取失败（未购买月卡或今日已领），已忽略",
                    gift.giftID,
                )
                continue
            ok_count += 1
            got.extend(res.gotList)

        label = config.GIFT_MONTHLY_LABEL
        if ok_count == 0:
            return f"{label}（{config.GIFT_MONTHLY_NOT_PURCHASED_NOTE}）"
        phrase = reward_phrase(got, scope=QuantityScope.HELD)
        tail = f"（{phrase}）" if phrase else "（服务端未回道具明细）"
        return f"{label}：已领取{tail}"

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉礼包列表"这一包，不发任何领取请求。"""
        import config

        packet = GetGiftListPacket.for_normal_store(
            platform=config.GIFT_STORE_PLATFORM,
            cash_type=config.GIFT_STORE_CASH_TYPE,
            channel_type=config.GIFT_STORE_CHANNEL_TYPE,
        )
        request = game.build(packet, include_defaults=True)
        print(request.describe())
        print()
        print("真实可领清单要等 37002 响应才能确定（本演练不联网）：")
        print("  · 37001 拉列表 → 挑出 isFree != 0 的礼包 → 对每个发 37019 领取")
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：37001 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )

