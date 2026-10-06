"""一般市集购买（主动任务）—— 把 4 种指定道具按"每日限购"买满。

【买什么 / 为什么买它】
``398000015`` 一般市集（``StoreSettingsData.storeID``）常年挂着这 4 样道具：

==============================  ====================  ==================================
itemID                          道具                  用途
==============================  ====================  ==================================
``200200000``                   血钻                  提升装备星等的矿石
``200200003``                   琥珀                  提升装备星等的矿石
``200600001``                   爱心巧克力            好感度 +50
``200600002``                   幸福香氛              好感度 +100
==============================  ====================  ==================================

结算货币是**金币**（``StoreSettingsData`` 里 ``398000015`` 那一行的
``currencyIcon = 200000050`` = ``COINS_ID``），所以本任务**不涉及钻石**，
自然也不需要"允许花钻石"总闸 —— 但它确实会花掉金币，所以界面上四项开关都标了
「消耗资源」，默认仍走手动执行的路径（``services/task_spec.py``）。

【流程】
::

    14001 GetStoreListPacket(storeType=398000015)（只读）→ 14002 货架
      └─ 按 itemID 匹配勾选的 4 种道具
      └─ 剩余可购量 remaining = max(0, maxBuyCount - nowBuyCount)
      └─ 对每个 remaining > 0 的商品：
           · 演练（--dry-run）→ 只打印"打算买几件、预计花多少金币"
           · 真跑            → 14003 BuyProductPacket(productSID, count=remaining) → 14004

【哪些情况**一定不发包**】
1. 用户把 4 个开关全部关掉（此时只做只读拉货架，顺便把货架现状报出来）；
2. 服务端货架上没有这件商品（可能被活动轮换掉了）；
3. ``nowBuyCount >= maxBuyCount``（今日买满 —— 需求里明确要求"买满则跳过"）；
4. ``--dry-run``（演练只组装、不发送）。

【为什么不重试、不并发】
``14003`` 一发出就真的扣金币；超时后重发等于"可能买了两遍"。
所以本任务只顺序发、每件最多发一次，失败即停并把服务端错误码原样报出来
（与 ``tasks/sweep.py`` 对 ``11009`` 的处理是同一套纪律）。

【顺带的好处】
``DailyMissionData`` 的"好貨等你買(I)/(II)"（市集購買 {0} 件商品）与
"採購達成"（每周）都以"购买件数"计数，所以这个任务同时把日常活跃也推进了。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from client.game_client import GameClient
from client.session import GameSession
from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.market import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    MARKET_CURRENCY_ITEM_ID,
    MARKET_ITEMS,
    MARKET_STORE_NAME,
    MARKET_STORE_TYPE,
    PACKET_PURPOSE,
    BuyProductPacket,
    BuyProductRes,
    GetStoreListPacket,
    GetStoreListRes,
    ProductClass,
)
from tasks.base import BaseTask, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.market_buy")


@register_task
class MarketBuyTask(BaseTask):
    """一般市集（398000015）购买：把勾选的目标道具按剩余可购量买满。

    :param buy_blood_diamond: 是否购买血钻（200200000）。
    :param buy_amber: 是否购买琥珀（200200003）。
    :param buy_chocolate: 是否购买爱心巧克力（200600001）。
    :param buy_fragrance: 是否购买幸福香氛（200600002）。
    """

    name: Final[str] = "market_buy"

    #: 需要登录态（两个包都要带会话令牌）
    requires_auth: Final[bool] = True

    #: 本任务**不存在花钻石的路径**：一般市集用金币结算
    #: （``StoreSettingsData`` 的 ``currencyIcon`` = 200000050 = 金币）。
    #: 显式声明是为了让 ``main.py tasks`` 如实显示"这个任务不花钻石"。
    spends_diamond: Final[bool] = False

    opt_in_hint: Final[str] = (
        "会消耗**金币**买满 4 种指定道具（每种每日限购，买满自动跳过）；"
        "演练模式只打印计划、不发 14003"
    )

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: Any = None,
        buy_blood_diamond: bool = True,
        buy_amber: bool = True,
        buy_chocolate: bool = True,
        buy_fragrance: bool = True,
    ) -> None:
        super().__init__(
            session, logger=logger, dry_run=dry_run, spend_policy=spend_policy
        )
        #: ``itemID → 是否购买``。**用 MARKET_ITEMS 的键来建表**，这样
        #: "界面上的开关名"与"协议里的 itemID"永远对得上；
        #: 将来有人改了道具编号，这里不必跟着改（改一处错一处是老手也会犯的错）。
        self.switches: dict[int, bool] = {
            200200000: bool(buy_blood_diamond),
            200200003: bool(buy_amber),
            200600001: bool(buy_chocolate),
            200600002: bool(buy_fragrance),
        }
        #: 本次**实际发出**的消息号（测试用它断言"没有越界发包"）
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def enabled_item_ids(self) -> tuple[int, ...]:
        """勾选的道具 ID（顺序 = :data:`models.market.MARKET_ITEMS` 的声明顺序）。"""
        return tuple(item_id for item_id in MARKET_ITEMS if self.switches.get(item_id))

    def switches_summary(self) -> str:
        """一行文字说明这次打算买哪几样（日志用）。"""
        return "、".join(
            f"{MARKET_ITEMS[item_id]}{'✔' if self.switches.get(item_id) else '✘'}"
            for item_id in MARKET_ITEMS
        )

    def _guarded_send(
        self, game: GameClient, packet: ProtoMessage, expect: type[ProtoMessage]
    ) -> EnvelopeView:
        """发送一个**白名单内**的包（越界 / 禁发直接抛错，绝不放行）。"""
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=_LOGGER,
        )

    # ------------------------------------------------------------------
    # 只读：拉货架
    # ------------------------------------------------------------------
    def fetch_store(self, game: GameClient) -> GetStoreListRes:
        """``14001``（只读）→ ``14002``：拉一般市集货架。"""
        view = self._guarded_send(
            game, GetStoreListPacket.for_market(), GetStoreListRes
        )
        return view.decode_sub_packet(GetStoreListRes)

    # ------------------------------------------------------------------
    # 演练（--dry-run）
    # ------------------------------------------------------------------
    def _dry_run(self) -> TaskResult:
        """演练：**不联网、不发包**，只把"真跑会发生什么"讲清楚。

        【为什么连只读的 14001 也不发】
        ``main.py`` 对 ``--dry-run`` 的承诺是"**不联网、不发包**"（与
        ``tasks/sweep.py::_dry_run`` 完全一致）。只读包虽然不扣钱，但它同样会
        把账号暴露在网关前 —— 既然用户要的是"先看一眼"，那就一个字节都不发。
        代价是演练读不到真实货架，所以这里如实写明"剩余量/单价要真跑才知道"。
        """
        enabled = self.enabled_item_ids()
        self.logger.info("演练模式：不会联网，也不会发送任何报文")
        self.logger.info(
            "白名单：%s（14001 拉货架 / 14003 购买）", sorted(ALLOWED_PACKET_IDS.values())
        )
        self.logger.info(
            "目标商铺：%s（storeType=%d，货币 %d = 金币）",
            MARKET_STORE_NAME,
            MARKET_STORE_TYPE,
            MARKET_CURRENCY_ITEM_ID,
        )
        self.logger.info("本次勾选：%s", self.switches_summary())
        if not enabled:
            self.logger.info("四种道具都没有勾选 → 真跑时也只会拉一次货架（只读）")
        else:
            for item_id in enabled:
                self.logger.info(
                    "真跑时会买满：%s（%d）—— 数量 = 服务端 maxBuyCount - nowBuyCount",
                    MARKET_ITEMS[item_id],
                    item_id,
                )
        self.logger.info("演练不发包，因此读不到真实剩余量与单价（这是刻意的）")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成（未联网、未发包）",
            data={
                "mode": "dry_run",
                "store_type": MARKET_STORE_TYPE,
                "enabled_items": [
                    {"item_id": item_id, "name": MARKET_ITEMS[item_id]}
                    for item_id in enabled
                ],
                "allowed_packets": sorted(ALLOWED_PACKET_IDS.values()),
                "sent_packets": (),
            },
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        if self.dry_run:
            return self._dry_run()
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        self.logger.info(
            "目标商铺：%s（storeType=%d，货币 %d = 金币）",
            MARKET_STORE_NAME,
            MARKET_STORE_TYPE,
            MARKET_CURRENCY_ITEM_ID,
        )
        self.logger.info("本次勾选：%s", self.switches_summary())

        res = self.fetch_store(game)
        if not res.is_success():
            msg = (
                f"拉取「{MARKET_STORE_NAME}」货架失败："
                f"storeType={MARKET_STORE_TYPE}，"
                f"errorCode={res.errorCode}（{res.describe_error()}）"
                f"（未发送任何购买请求）"
            )
            self.logger.error("%s", msg)
            return TaskResult(
                task=self.name,
                ok=False,
                message=msg,
                data={
                    "error_code": res.errorCode,
                    "error_text": res.describe_error(),
                    "store_type": MARKET_STORE_TYPE,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

        self.logger.info("货架：%s", res.summary())
        # 把 4 件目标道具的现状逐条打出来（买没买、为什么没买，都在这里看得见）
        for item_id, name in MARKET_ITEMS.items():
            product = res.find_product(item_id)
            if product is None:
                # 裸 itemID 不进运行视图（用户看名字就够）—— 见「结果视图纪律」
                self.logger.info("  · %s：货架上没有这件商品", name)
            else:
                mark = "✔ " if self.switches.get(item_id) else "（未勾选）"
                self.logger.info("  · %s%s", mark, product.describe())

        enabled = self.enabled_item_ids()
        if not enabled:
            msg = "四种道具都没有勾选：本次只拉了一次货架（只读），未发送任何购买请求"
            self.logger.info("%s", msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "mode": "read_only",
                    "shelf": [product.describe() for product in res.productList],
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

        plan, skipped = self._build_plan(res, enabled)
        for note in skipped:
            self.logger.info("跳过：%s", note)

        if not plan:
            tail = "；".join(skipped) if skipped else "没有可购买的目标商品"
            msg = f"今日已全部售罄（或商品未上架）：{tail}。未发送任何购买请求"
            self.logger.info("%s", msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "mode": "nothing_to_buy",
                    "skipped": skipped,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

        estimated_cost = sum(product.price * count for product, count in plan)

        return self._buy_all(game, plan, estimated_cost)


    # ------------------------------------------------------------------
    # 计划：谁还需要买、买几件
    # ------------------------------------------------------------------
    def _build_plan(
        self, res: GetStoreListRes, enabled: tuple[int, ...]
    ) -> tuple[list[tuple[ProductClass, int]], list[str]]:
        """算出本次的下单清单与"被跳过"的原因清单。

        :return: ``([(商品, 数量), …], ["被跳过的原因", …])``。

        【为什么"是否下单"只看服务端的两个字段】
        ``nowBuyCount`` / ``maxBuyCount`` 是服务端每天现算的；本地记忆
        （上一次跑到哪了）会因为"手机上手动买过""跨设备"而失效。
        """
        plan: list[tuple[ProductClass, int]] = []
        skipped: list[str] = []
        for item_id in enabled:
            name = MARKET_ITEMS.get(item_id, str(item_id))
            product = res.find_product(item_id)
            if product is None:
                skipped.append(f"{name}：货架上没有这件商品（可能已轮换下架）")
                continue
            count = product.remaining()
            if count <= 0:
                skipped.append(
                    f"{name}：今日已买满（{product.nowBuyCount}/{product.maxBuyCount}）"
                )
                continue
            plan.append((product, count))
        return plan, skipped

    # ------------------------------------------------------------------
    # 真买（有副作用）
    # ------------------------------------------------------------------
    def _buy_all(
        self,
        game: GameClient,
        plan: list[tuple[ProductClass, int]],
        estimated_cost: int,
    ) -> TaskResult:
        """顺序购买清单里的每一件商品；**成功一件记一件，失败即停（不重试）**。"""
        purchased: list[dict[str, Any]] = []
        errors: list[str] = []

        for index, (product, count) in enumerate(plan, start=1):
            # ★ 2026-10-04 口径修正：原来这里印的是
            # ``%s（productSID）×%d，单价 %d（货币 currencyType）`` ——
            # 两个问题：① productSID 是**商品编号**，不是名称；
            # ② currencyType 是**货币道具 ID**，印出来会被读成价格
            #（"单价 500（货币 200000050）"看起来像"要花两亿"）。
            # 现在只用中文名，编号一律降到 DEBUG。
            self.logger.info(
                "第 %d/%d 件：%s ×%d，单价 %d %s",
                index,
                len(plan),
                product.display_name(),
                count,
                product.price,
                product.currency_name(),
            )
            self.logger.debug(
                "第 %d/%d 件商品编号 productSID=%s，货币道具 ID=%s",
                index,
                len(plan),
                product.productSID,
                product.currencyType,
            )
            view = self._guarded_send(
                game,
                BuyProductPacket(productSID=product.productSID, count=count),
                BuyProductRes,
            )
            res = view.decode_sub_packet(BuyProductRes)
            if not res.is_success():
                reason = (
                    f"{product.display_name()}购买失败："
                    f"errorCode={res.errorCode}（{res.describe_error()}）"
                )
                self.logger.error("%s", reason)
                errors.append(reason)
                break  # 有副作用的请求失败不重试：停下来把服务端原话报给用户

            purchased.append(
                {
                    "item_id": product.itemID,
                    "name": product.display_name(),
                    "product_sid": product.productSID,
                    "count": count,
                    "now_buy_count": res.nowBuyCount,
                }
            )
            # 14004 的 itemList/costList 数量口径**尚未实证**（同源 6006/11010/37020
            # 已证实是"操作后持有量"），所以明细只进开发者日志，不进运行视图。
            self.logger.debug(
                "  ✔ 购买成功：%s；%s（该商品今日累计 %d 件）",
                res.item_summary(),
                res.cost_summary(),
                res.nowBuyCount,
            )

        if errors:
            msg = f"已购买 {len(purchased)}/{len(plan)} 件：{errors[0]}（已停止，不重试）"
            return TaskResult(
                task=self.name,
                ok=False,
                message=msg,
                data={
                    "mode": "buy",
                    "purchased": purchased,
                    "errors": errors,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

        names = "、".join(str(item["name"]) for item in purchased)
        msg = f"已买满 {len(purchased)} 种道具（{names}），预计消耗金币约 {estimated_cost}"
        self.logger.info("%s", msg)
        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "mode": "buy",
                "purchased": purchased,
                "estimated_cost": estimated_cost,
                "sent_packets": tuple(self.sent_packet_ids),
            },
        )

