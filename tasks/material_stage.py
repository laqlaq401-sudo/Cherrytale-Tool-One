"""素材关卡与元素试炼扫荡与首通任务。

【业务说明】
日常素材与元素试炼关卡扫荡任务（不消耗体力，消耗每日次数）：
1. 支持按类别（job / element / all）拉取子目录清单；
2. 支持指定区域（area）和关卡（section），未指定关卡时自动选择该区最高难度；
3. 支持首次通关开关（allow_unpassed）：对 cheatCheck=0 关卡安全首通以解锁后续扫荡；
4. 严格守护安全底线，禁发购买次数或购买体力报文。

【★ 2026-10-03 修订：扫荡券规则按素材抓包重写（推翻 10-02 的两条结论）】
- ``captures/素材关卡扫荡.saz``（10.3）证明：**素材/元素关**的扫荡
  ``useSweepTicket`` 固定发 **0**、不发 2007 查券、响应 costList 为空
  （不消耗券）。10-02 时"实测客户端固定发 1"的结论来自**降临活动**抓包，
  只适用于 :mod:`tasks.sweep`；两类关卡是两套规则，各自钉死在 config：
  ``SWEEP_USE_TICKET_VALUE=1``（活动）/ ``MATERIAL_SWEEP_USE_TICKET_VALUE=0``（素材）。
- 因此本任务**不再做"0 张券拒扫"的闸门**（那是给消耗券的扫荡准备的），
  也不再把券数当成削峰依据；2007 查券代码保留但只在
  ``MATERIAL_SWEEP_USE_TICKET_VALUE == 1`` 时才会走到（当前恒为 0）。
- 保留不变的：官方档位归一（1 / 5 / 10）、首通握手、禁发购买报文。

【★ 2026-10-04 修复：11009 漏发「值为 0 的字段」→ 服务端回 22 字节 ack】
真实客户端扫素材关时，11009 子包是 **9 B** ``{sectionID, times, useSweepTicket=0}``
（``captures/素材关卡扫荡.saz`` 的 ``raw/08 / raw/10 / raw/12``）—— ``useSweepTicket``
虽然等于 0 也**照发**。本任务重写时漏了把 ``include_defaults=True`` 传下去
（``guarded_send`` 默认 False），实际上网只剩 **7 B**，服务端判定"字节形状不符"，
回 22 B ``{"response_code":"OK"}``（``notes/glory_summit_capture.md`` 第九节的情形①）。
修复：``_guarded_send`` 增加 ``include_defaults`` 透传，**只有 11009 传 True**；
11001 仍必须 False（抓包 ``raw/02`` 是 11 B，``regionID=0`` 被客户端省略）。
"""

from __future__ import annotations

import logging
from typing import Any, Final

import config
from client.game_client import GameClient
from client.session import GameSession
from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.material_stage import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    FightDataClass,
    FightingRequestPacket,
    FightingResponseRes,
    FightingResultPacket,
    FightingResultRes,
    GetAreaByDifficultyPacket,
    GetAreaByDifficultyRes,
    MaterialArea,
    MaterialSection,
    SweepSectionPacket,
    SweepSectionRes,
    get_material_areas,
    make_battle_server_verify,
    resolve_material_area,
    resolve_material_section,
)
from models.activity_stage import (
    SWEEP_TICKET_ITEM_ID,
    SWEEP_UNTIL_EMPTY,
    describe_error_code,
    normalize_sweep_times,
)
from models.wallet import GetAllItemPacket, GetAllItemRes
from services import cancellation
from tasks.base import BaseTask, TaskResult, count_item_kinds, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.material_stage")

#: 「扫荡到次数耗尽」里"**预期内**"的停止码：命中这些不是程序错误，而是"扫不动了"
#: 的自然收尾（素材关最典型的是 ``-7 TimesIsFull`` 每日次数已满）。
UNTIL_EXHAUSTED_NATURAL_STOP: Final[frozenset[int]] = frozenset(
    {
        -1,  # NoClearance   未通关
        -2,  # NoEnergy      体力不足（素材关不该出现，防御性保留）
        -3,  # NoTicket      券不足（素材关不该出现，防御性保留）
        -7,  # TimesIsFull   次数已满 ← 素材关的正常收尾
        -8,  # DailyClose    今日关闭
        -11,  # NoOpenForSweep 该关卡未开放扫荡
    }
)


@register_task
class SweepMaterialTask(BaseTask):
    """素材关卡与元素试炼扫荡与首通任务。"""

    name: Final[str] = "sweep_material"
    requires_auth: Final[bool] = True
    spends_diamond: Final[bool] = False

    def __init__(
        self,
        session: GameSession | None = None,
        *,
        category: str = "all",
        area: str | None = None,
        times: int = 0,
        section: str | None = None,
        allow_unpassed: bool = False,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session=session or GameSession(),
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
        )
        self.category = (category or "all").strip().lower()
        self.area_query = area.strip() if isinstance(area, str) and area.strip() else None
        #: ★ 2026-10-04：「扫荡到次数耗尽」模式（哨兵 ``-1``）。
        #: 【为什么必须先判断哨兵】素材关不消耗体力、只消耗每日次数，所以这里的
        #: 哨兵含义是"扫到次数耗尽"（服务端回 ``TimesIsFull = -7``），不是"清空体力"。
        #: 同样地，下面的 ``max(..., 0)`` 会把 -1 吞成 0（= 只读），所以要先算。
        raw_times = int(times or 0)
        self.until_exhausted = raw_times == SWEEP_UNTIL_EMPTY
        self.times = 0 if self.until_exhausted else max(raw_times, 0)
        self.section_query = (
            section.strip() if isinstance(section, str) and section.strip() else None
        )
        self.allow_unpassed = bool(allow_unpassed)

    def emit(self, msg: str) -> None:
        self.logger.info(msg)

    def _guarded_send(
        self,
        game: GameClient,
        packet: ProtoMessage,
        expect: type[ProtoMessage],
        *,
        include_defaults: bool = False,
    ) -> EnvelopeView:
        """发送一个**白名单内**的包（本任务唯一的发包出口）。

        :param include_defaults: 是否把「值等于默认值（0 / ``False`` / ``""``）」的字段
            也发出去。**必须按包决定，不能整个任务一刀切** —— 实测客户端的规则是
            "字段带 ``[DefaultValue]`` 才省略"（见 ``notes/glory_summit_capture.md`` 第九节）：

            - ``11009 SweepSectionPacket`` → **必须 True**。10.3 抓包 ``raw/08/10/12``
              的子包是 9 B（``useSweepTicket=0`` 照发）；省略它只剩 7 B，
              服务端会回 22 B ``{"response_code":"OK"}`` ack
              （**2026-10-04 素材扫荡事故的根因**，见本模块文档末尾与
              ``tasks/packet_guard.py`` 的提示文本）；
            - ``11001 GetAreaByDifficultyPacket`` → **必须 False**。抓包 ``raw/02``
              的子包是 11 B，只有 ``difficultyType=-3`` —— ``regionID=0`` 被客户端省略；
            - ``11015 / 11017``（首通）→ 暂无抓包，保持 False（口径未定，见 Q-049）。

        【为什么默认 False 而不是 True】
        多发客户端本来省略的字段，服务端同样可能判包异常（历史上出现过
        ``ABNORMAL_PACKET``）—— 所以这里是"显式开关"，只有抓包证明"客户端会发"的
        包才打开。
        """
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            purpose=PACKET_PURPOSE,
            logger=_LOGGER,
            include_defaults=include_defaults,
        )

    def _query_ticket_count(self, game: GameClient) -> int:
        """``2007 GetAllItemPacket``（空包）→ ``2008``：数出背包里的**扫荡券**。

        :return: 当前持有的扫荡券张数（背包里一条都没有时返回 ``0``）。

        【为什么现在几乎不会被调到】
        10.3 素材抓包证明素材扫荡 ``useSweepTicket=0``、不消耗券，所以
        "先查券再扫"的流程被移除；这个方法只在
        ``config.MATERIAL_SWEEP_USE_TICKET_VALUE == 1`` 时才会被走到
        （万一将来实测口径变化，改一个常量即可恢复"先读后发"的闸门）。
        """
        view = self._guarded_send(game, GetAllItemPacket(), GetAllItemRes)
        res = view.decode_sub_packet(GetAllItemRes)
        ticket_left = max(int(res.amount_of(SWEEP_TICKET_ITEM_ID)), 0)
        self.emit(
            f"扫荡券：当前持有 {ticket_left} 张（每扫 1 次扣 1 张）"
        )
        self.logger.debug("扫荡券 itemID=%d", SWEEP_TICKET_ITEM_ID)
        return ticket_left

    # ------------------------------------------------------------------
    # 「扫荡到次数耗尽」：反复发「扫荡 5 次」，直到服务端回 TimesIsFull(-7)
    # ------------------------------------------------------------------
    #: 每一轮发的次数。固定 **5** —— 与活动关清空模式同一口径（一次操作 = 一个
    #: ``times=5`` 的包；素材抓包 raw/08/10/12 就是连点三次"扫荡 5 次"）。
    UNTIL_EXHAUSTED_ROUND_TIMES: Final[int] = 5

    def _sweep_until_exhausted(
        self, game: GameClient, area: MaterialArea, section: MaterialSection
    ) -> TaskResult:
        """对**指定关卡**反复发「扫荡 5 次」，直到服务端回 ``TimesIsFull = -7``。

        【与活动关「清空体力」的关系】执行方式完全一致（每轮一个 ``times=5`` 的包、
        轮间隔随机 1~2 秒、靠服务端裁决停止），差别只在**停止信号**：素材关不消耗体力，
        消耗的是每日挑战次数，所以收尾是服务端回 ``-7``，而不是体力不足。

        【为什么用 -7 当停止信号】素材流程不发 11003（对素材区是无效查询），本地拿不到
        "今日还剩几次"，所以"服务端拒绝"就是唯一可靠的收尾依据（与抓包一致：
        素材抓包 raw/08/10/12 是三个连发的 ``times=5``，服务端全程回成功）。

        【安全上限】``config.SWEEP_UNTIL_EMPTY_MAX_TIMES``（默认 200）兜住
        "服务端异常、永远回成功"的极端情况。
        """
        if self.dry_run:
            msg = (
                f"【演练】对关卡 {section.name} 反复执行「扫荡 "
                f"{self.UNTIL_EXHAUSTED_ROUND_TIMES} 次」直到次数耗尽，"
                "演练模式跳过实际发包"
            )
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "area_id": area.area_id,
                    "section_id": section.section_id,
                    "mode": "dry_run_until_exhausted",
                },
            )

        # 素材关实测 useSweepTicket=0（不查券、不扣券）—— 仍读 config，
        # 将来若口径变化改一个常量即可。
        use_ticket = config.MATERIAL_SWEEP_USE_TICKET_VALUE
        self.emit(
            f"扫荡到次数耗尽：对关卡 {section.name} 反复执行「扫荡 "
            f"{self.UNTIL_EXHAUSTED_ROUND_TIMES} 次」，直到服务端返回次数已满"
            f"（安全上限 {config.SWEEP_UNTIL_EMPTY_MAX_TIMES} 次、"
            f"轮间隔随机 {config.SWEEP_INTERVAL_MIN_SECONDS:.1f}~"
            f"{config.SWEEP_INTERVAL_MAX_SECONDS:.1f}s）"
        )

        rounds: list[tuple[int, SweepSectionRes]] = []
        total = 0
        reason = f"已达到安全上限 {config.SWEEP_UNTIL_EMPTY_MAX_TIMES} 次，已停止"
        while True:
            # 每一轮都是安全的中止点（用户点「停止执行」后最多再扫完手上这一轮）。
            cancellation.raise_if_cancelled()
            if total >= config.SWEEP_UNTIL_EMPTY_MAX_TIMES:
                break
            round_times = min(
                self.UNTIL_EXHAUSTED_ROUND_TIMES,
                config.SWEEP_UNTIL_EMPTY_MAX_TIMES - total,
            )
            # ★ 11009 必须 include_defaults=True：useSweepTicket=0 也要照发，
            #   否则服务端判"字节形状不符"回 22B ack（见本模块 2026-10-04 事故）。
            view = self._guarded_send(
                game,
                SweepSectionPacket(
                    sectionID=section.section_id,
                    times=round_times,
                    useSweepTicket=use_ticket,
                ),
                SweepSectionRes,
                include_defaults=True,
            )
            res = view.decode_sub_packet(SweepSectionRes)
            rounds.append((round_times, res))
            self.emit(
                f"第 {len(rounds)} 轮：扫 {round_times} 次 → {res.cost_summary()}"
            )
            if not res.is_success:
                if res.errorCode == -7:
                    reason = "每日次数已耗尽（TimesIsFull=-7），正常收尾"
                else:
                    reason = (
                        f"服务端返回错误码 {res.errorCode}"
                        f"（{describe_error_code(res.errorCode)}），已停止（不重试）"
                    )
                break
            total += round_times
            cancellation.sleep_interruptible(config.sweep_interval())

        last = rounds[-1][1] if rounds else None
        # 成功收尾（含撞安全上限时最后一轮成功）或命中"预期内"停止码 → 任务算成功。
        ok = (
            last is None
            or last.is_success
            or last.errorCode in UNTIL_EXHAUSTED_NATURAL_STOP
        )
        item_kinds = (
            count_item_kinds(last.settlement_items()) if last is not None else 0
        )
        message = (
            f"扫荡到次数耗尽完成：对关卡 {section.name} 共扫 {total} 次"
            f"（{len(rounds)} 轮 × 每轮最多 {self.UNTIL_EXHAUSTED_ROUND_TIMES} 次）："
            f"{reason}"
        )
        self.emit(message)
        return TaskResult(
            task=self.name,
            ok=ok,
            message=message,
            data={
                "mode": "sweep_until_exhausted",
                "area_id": area.area_id,
                "section_id": section.section_id,
                "rounds": len(rounds),
                "times_total": total,
                "item_kinds": item_kinds,
                "stop_reason": reason,
                "last_error_code": (last.errorCode if last is not None else None),
                "packets": [
                    {"round": index + 1, "times": times, "error_code": res.errorCode}
                    for index, (times, res) in enumerate(rounds)
                ],
            },
        )

    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        # 1. 如果未指定区域，拉取并展示分类子目录
        if not self.area_query:
            return self._list_catalog(game)

        # 2. 定位区域与关卡
        area = resolve_material_area(self.area_query, self.category)
        if area is None:
            msg = f"未找到匹配的素材关卡区域: {self.area_query!r}（大类: {self.category}）"
            self.emit(msg)
            return TaskResult(task=self.name, ok=False, message=msg)

        target_section: MaterialSection | None = None
        if self.section_query:
            res = resolve_material_section(self.section_query, area)
            if res is None:
                msg = f"在区域【{area.name}】中未找到指定关卡: {self.section_query!r}"
                self.emit(msg)
                return TaskResult(task=self.name, ok=False, message=msg)
            _, target_section = res
        else:
            target_section = area.get_highest_section()

        self.emit(f"选中目标: 【{area.category_name}】{area.name} -> {target_section.name} (ID: {target_section.section_id})")

        # 3. ★ 11001 拉服务端区域清单，校验目标区域今日是否开放（2026-10-03 重写）
        # 10.3 素材抓包：真实客户端是 11001（difficultyType=-3）→ 11009 直接扫，
        # **不发 11003**（那是对素材区发空子包的无效查询）。所以：
        # - 用 11002 的区域清单做"区域是否开放"的校验；
        # - 不再用 11003 预判"是否首通"—— 直接扫，让 11009 的 errorCode=-1
        #   （未通关）告诉我们，再按 allow_unpassed 决定是否首通后重扫。
        self.emit("正在拉取服务端素材区域清单 (11001)...")
        area_view = self._guarded_send(
            game, GetAreaByDifficultyPacket(difficultyType=-3), GetAreaByDifficultyRes
        )
        area_res = area_view.decode_sub_packet(GetAreaByDifficultyRes)
        server_area_ids = {item.areaID for item in area_res.areaStateList}
        if area.area_id not in server_area_ids:
            msg = (
                f"区域【{area.name}】(ID: {area.area_id}) 不在服务端当前的素材区清单里"
                f"（共 {len(server_area_ids)} 个区）—— 区域清单会轮换，请换个区域或重新确认"
            )
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=False,
                message=msg,
                data={
                    "area_id": area.area_id,
                    "server_area_ids": sorted(server_area_ids),
                },
            )
        self.emit(f"区域已在服务端清单中（当前共 {len(server_area_ids)} 个素材区）")

        # 4. 首通逻辑不再前置预判：先扫，扫出"未通关"再按开关处理（见 _sweep）
        is_passed = True  # 乐观假设；11009 errorCode=-1 时由 _sweep 纠正

        # 4.5 ★ 2026-10-04：「扫荡到次数耗尽」走**独立循环**（反复发 times=5，
        #     直到服务端回 TimesIsFull=-7）。它必须放在 times<=0 的只读判断**之前** ——
        #     因为该模式下 self.times 恒为 0（真正的次数在循环里逐轮算）。
        if self.until_exhausted:
            return self._sweep_until_exhausted(game, area, target_section)

        # 5. 扫荡逻辑
        if self.times <= 0:
            msg = f"关卡 {target_section.name} 状态查询完毕（只读模式，扫荡次数为 0）"
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "area_id": area.area_id,
                    "section_id": target_section.section_id,
                    "passed": is_passed,
                },
            )

        if self.dry_run:
            msg = f"【演练】对关卡 {target_section.name} 执行扫荡 {self.times} 次，演练模式跳过实际发包"
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "area_id": area.area_id,
                    "section_id": target_section.section_id,
                    "times": self.times,
                    "dry_run": True,
                },
            )

        # 6. ★ 官方档位归一 + 扫荡券处理（2026-10-03 按素材抓包重写）
        # 档位归一保留（客户端面板只有 1 / 5 / 10 三档）；扫荡券处理改为按
        # config.MATERIAL_SWEEP_USE_TICKET_VALUE（实测 0）分流：取 0 时不查券、
        # 不设闸门、不削峰 —— 与真实客户端"不数券直接扫"完全一致。
        planned_times, tier_note = normalize_sweep_times(self.times)
        if tier_note:
            self.emit(f"档位归一：{tier_note}")

        if planned_times <= 0:
            msg = (
                "按官方档位（1 / 5 / 10）归一后，本次可扫次数为 0，"
                "未发送任何扫荡请求"
            )
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "mode": "nothing_to_sweep",
                    "section_id": target_section.section_id,
                },
            )

        use_ticket = config.MATERIAL_SWEEP_USE_TICKET_VALUE
        ticket_left: int | None = None
        if use_ticket == 1:
            # 只有"用券的扫荡"才需要先数券、按券削峰（当前素材关恒不走这支）。
            ticket_left = self._query_ticket_count(game)
            if ticket_left <= 0:
                msg = (
                    "扫荡券不足（当前持有 0 张），拒绝执行扫荡："
                    "本次不会发送任何 11009（扫荡每 1 次消耗 1 张扫荡券，先领券再来）"
                )
                self.logger.warning("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={
                        "mode": "no_ticket",
                        "section_id": target_section.section_id,
                        "ticket_left": 0,
                    },
                )

            if ticket_left < planned_times:
                capped, cap_note = normalize_sweep_times(ticket_left)
                self.emit(
                    f"扫荡券只有 {ticket_left} 张 → 按官方档位降到 {capped} 次"
                    + (f"（{cap_note}）" if cap_note else "")
                )
                planned_times = capped

        self.emit(
            f"正在对关卡 {target_section.name} 扫荡 {planned_times} 次"
            f"（useSweepTicket={use_ticket}）..."
        )
        # ``useSweepTicket`` 读 config 里的**分类实测值**：素材关 0、活动关 1
        # （见 config 两个常量上的注释，依据分别是 10.3 素材抓包与降临抓包）。
        sweep_req = SweepSectionPacket(
            sectionID=target_section.section_id,
            times=planned_times,
            useSweepTicket=use_ticket,
        )
        # ★ 11009 必须 include_defaults=True：抓包 raw/08 的子包是 9 B
        # ``{sectionID, times, useSweepTicket=0}``（0 值字段照发）；发 7 B（省略
        # useSweepTicket）会被服务端当"字节形状不符"回 22 B ack（2026-10-04 事故）。
        sweep_view = self._guarded_send(
            game, sweep_req, SweepSectionRes, include_defaults=True
        )
        sweep_res = sweep_view.decode_sub_packet(SweepSectionRes)

        is_ok = sweep_res.is_success() if callable(sweep_res.is_success) else bool(sweep_res.is_success)
        if not is_ok or sweep_res.errorCode != 0:
            # ★ 未通关（errorCode=-1）：按开关回退首通 → 重扫一次（2026-10-03 新流程）
            if sweep_res.errorCode == -1:
                if not self.allow_unpassed:
                    msg = (
                        f"关卡 {target_section.name} 尚未首通（服务端 errorCode=-1），"
                        "当前未开启首通允许（--allow-unpassed），跳过扫荡"
                    )
                    self.emit(msg)
                    return TaskResult(
                        task=self.name,
                        ok=True,
                        message=msg,
                        data={
                            "section_id": target_section.section_id,
                            "passed": False,
                            "error_code": -1,
                        },
                    )
                self.emit(f"检测到关卡 {target_section.name} 未首通，正在执行首通挑战...")
                first_pass_ok = self._do_first_pass(game, target_section)
                if not first_pass_ok:
                    msg = f"关卡 {target_section.name} 模拟首通失败"
                    self.emit(msg)
                    return TaskResult(
                        task=self.name,
                        ok=False,
                        message=msg,
                        data={"section_id": target_section.section_id},
                    )
                self.emit(f"关卡 {target_section.name} 首通成功，重新扫荡...")
                # 首通后重扫同样是 11009 → 同样必须 include_defaults=True
                sweep_view = self._guarded_send(
                    game, sweep_req, SweepSectionRes, include_defaults=True
                )
                sweep_res = sweep_view.decode_sub_packet(SweepSectionRes)
                is_ok = sweep_res.is_success() if callable(sweep_res.is_success) else bool(sweep_res.is_success)
                if not is_ok or sweep_res.errorCode != 0:
                    msg = f"首通后重扫仍失败 (errorCode={sweep_res.errorCode})"
                    self.emit(msg)
                    return TaskResult(
                        task=self.name,
                        ok=False,
                        message=msg,
                        data={"error_code": sweep_res.errorCode},
                    )
            else:
                msg = f"扫荡关卡 {target_section.name} 失败 (errorCode={sweep_res.errorCode})"
                self.emit(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={"error_code": sweep_res.errorCode},
                )

        # ★ 2026-10-04 单位/口径修正：
        # ① ``settlement_items()`` 返回的是**一件一件的道具**，``len()`` 是**条数**，
        #    说成"种掉落物"不准（同一 itemID 出现两次会被算成两种）→ 改用
        #    count_item_kinds（去重后的 itemID 个数）。
        # ② 11010 的 itemAmount 是**扫荡后持有量**（Q-021 真机结案），
        #    所以运行视图只报"种数"，**绝不报数量**。
        item_kinds = count_item_kinds(sweep_res.settlement_items())
        result_data: dict[str, Any] = {
            "area_id": area.area_id,
            "section_id": target_section.section_id,
            "times": planned_times,
            "item_kinds": item_kinds,
            "use_sweep_ticket": use_ticket,
        }
        if use_ticket == 1 and ticket_left is not None:
            # ★ 用券的扫荡才报券的消耗（响应里有就用响应里的，没有就按
            # "每扫 1 次扣 1 张"自己算）。素材扫荡（use_ticket=0）不涉及券。
            reported_ticket = sweep_res.sweep_ticket_left
            ticket_after = (
                max(int(reported_ticket), 0)
                if reported_ticket is not None
                else max(ticket_left - planned_times, 0)
            )
            result_data["ticket_left"] = ticket_left
            result_data["ticket_after"] = ticket_after
            msg = (
                f"成功扫荡关卡 {target_section.name} {planned_times} 次，"
                f"获得 {item_kinds} 种掉落物（扫荡券 {ticket_left} → {ticket_after}）"
            )
        else:
            msg = (
                f"成功扫荡关卡 {target_section.name} {planned_times} 次，"
                f"获得 {item_kinds} 种掉落物"
            )
        self.emit(msg)
        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data=result_data,
        )

    def _list_catalog(self, game: GameClient) -> TaskResult:
        """只读拉取关卡目录与状态，输出结构化清单。"""
        areas = get_material_areas(self.category)
        self.emit(f"=== 素材关卡与元素试炼目录（分类: {self.category}，共 {len(areas)} 个区域）===")

        areas_data: list[dict[str, Any]] = []
        for a in areas:
            self.emit(f"[{a.category_name}] 编号 {a.display_id}: {a.name}（共 {len(a.sections)} 关）")
            for s in a.sections:
                self.emit(f"  └ 关卡 {s.display_id}: {s.name} (等级限制: {s.level_limit})")
            areas_data.append(a.as_view())

        msg = f"成功拉取素材关卡子目录清单（{len(areas)} 个区域，共 {sum(len(a.sections) for a in areas)} 关卡）"
        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={"category": self.category, "areas": areas_data},
        )

    def _do_first_pass(self, game: GameClient, section: MaterialSection) -> bool:
        """模拟首通 cheatCheck=0 关卡。"""
        # 1. 创建战斗 Token (11015)
        token_req = FightingRequestPacket(sectionID=section.section_id, teamID=1)
        token_view = self._guarded_send(game, token_req, FightingResponseRes)
        token_res = token_view.decode_sub_packet(FightingResponseRes)

        if not token_res.is_success() or not token_res.fightToken:
            _LOGGER.error("创建战斗 Token 失败: errorCode=%d", token_res.errorCode)
            return False

        fight_token = token_res.fightToken

        # 2. 构造校验并提交战斗结算 (11017)
        verify = make_battle_server_verify(section_id=section.section_id)
        over_req = FightingResultPacket(
            fightData=FightDataClass(
                token=fight_token,
                battleServerVerify=verify,
            ),
            winLose=1,
            starCount=[1, 1, 1],
            timeCostSec=15,
        )
        over_view = self._guarded_send(game, over_req, FightingResultRes)
        over_res = over_view.decode_sub_packet(FightingResultRes)

        return over_res.is_success()
