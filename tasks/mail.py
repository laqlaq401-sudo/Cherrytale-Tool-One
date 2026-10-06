"""邮件拉取与一键领取（消息号 13001 → 13002，再 13003 → 13004）。

【这个任务只做两件事】

    13001 MailGetListPacket（空报文）    → 13002 MailGetListRes：拿到邮箱里有哪些邮件
    13003 MailOpenParcelPacket{mailIDList} → 13004 MailOpenParcelRes：一次性全部领掉

所以流程是**固定的两步、共两个请求**：先拉列表拿 ID，再把 ID 列表发回去领奖。
协议本身把"批量领取"做成了单请求，因此这里**没有循环领取**。

【为什么不读邮件正文】

奖励的权威来源是 13004 的 ``addList``（服务端实际发了什么），
而不是邮件正文里的附件展示。正文（标题 / 发件人 / 附件预览）是给玩家看的，
自动化要的是"东西到手"。少解析一层就少一处会出错的地方，
代码也短得多 —— 详见 ``models/mail.py`` 的模块文档。

【失败时会发生什么】

- 网络问题 → 抛 ``NetworkError``，由 :meth:`tasks.base.BaseTask.run` 转成失败结果；
- 服务端回业务错误码 → :meth:`MailTask.claim_all_mails` 抛 ``ApiError``（带上原始
  ``errorCode``），同样由基类转成失败结果。

**两者都不重试**：领取有副作用（服务端可能已经发过奖），重试只会再被拒一次，
还可能触发风控 —— 与 ``DMReward`` / ``GetFreeGiftPacket`` 同一原则。

【★ 2026-10-04 结果视图纪律（本任务的核心口径）】

**运行视图只说"领了几封"，不说"领到了什么"。**

    一键领取 3 封邮件成功
    逐封领取：成功 3 封、失败 2 封（多为已读 / 已领过）

理由有两条，都不是"想少写几个字"：

1. **6006/13004 的 ``itemAmount`` 是"领取后持有量"**（Q-021 真机结论，
   见 ``models/reward.py`` 的模块文档）—— 把它当"获得"念出来，
   用户会看到"金币×3661 万"这种账户级数字，误以为一封邮件发了这么多。
   要报真实增量就得领取前后各发一次 2007 求差，那是多发只读包的额外成本；
2. 用户要的是"有没有领到"，而不是"附件清单"。

所以本任务的结果文案**不含道具明细**，``data`` 里也不再放明细文本。
**开发者日志（DEBUG）保留全部明细**：逐封的 ``mailID``、
``res.reward_summary()`` 都在 ``logger.debug`` 里 ——
需要排查"到底哪封没领到"时开 ``--verbose`` 即可。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Final

from client.exceptions import ApiError, CherrytaleError
from client.game_client import GameClient
from models.daily import GetObjClass
from models.envelope import EnvelopeError, EnvelopeView
from models.mail import (
    MailGetListPacket,
    MailGetListRes,
    MailOpenParcelPacket,
    MailOpenParcelRes,
)
from tasks.base import BaseTask, TaskResult, register_task

#: 逐封领取（批量被拒后的降级路径）时，每封之间的间隔（秒）。
#: 5 封邮件快发也就快 1.5 秒，却要多挨几次风控扫描 —— 不值。
SINGLE_CLAIM_INTERVAL_SECONDS: Final[float] = 0.3


@dataclass
class SingleClaimOutcome:
    """逐封领取的汇总结果。

    :param claimed: **成功封数**（注意：不等于 ``len(rewards)`` —— 一封邮件的
        ``addList`` 可能有多组、也可能一组都没有，所以封数必须单独计数）。
    :param rewards: 成功领取到的奖励（各封的 ``addList`` 合并，顺序 = 领取顺序）。

        .. note::
           **只供开发者日志与测试使用**。13004 的 ``itemAmount`` 是"领取后持有量"
           （Q-021），不能当"获得"念给用户听 —— 运行视图只报封数，
           见模块文档的「结果视图纪律」。
    :param failed: 失败的 ``(mailID, errorCode)`` 列表；``errorCode`` 为 ``None``
        表示那封是网络/协议层面就失败了（连业务码都没拿到）。

    【为什么不只返回一个奖励列表】
    调用方需要同时知道"领到了几封"与"哪几封没成" —— 只返回奖励列表的话，
    "5 封里只领到 3 封"会被悄悄说成"领取成功"，而那正是本项目最忌讳的
    "成功但没奖励"式失败。
    """

    claimed: int = 0
    rewards: list[GetObjClass] = field(default_factory=list)
    failed: list[tuple[int, int | None]] = field(default_factory=list)

    def failure_text(self) -> str:
        """把失败清单写成一行中文（日志/结果消息用）。"""
        return "、".join(
            f"{mail_id}（{'网络/协议失败' if code is None else f'errorCode={code}'}）"
            for mail_id, code in self.failed
        )


@register_task
class MailTask(BaseTask):
    """拉取邮件列表 → 一键领取全部邮件奖励。

    【两条关键策略（都由 2026-09-22 的真机实证逼出来）】

    1. **只领未读邮件**（:meth:`MailGetListRes.unread_mail_ids`）：
       批量 13003 里只要混进一封已读邮件，服务端就回 ``errorCode=-2``、
       **一封都不发**。玩家在游戏里点"一键领取"时也只挑有红点的那些，
       我们照做即可。
    2. **批量被拒 → 逐封降级**（:meth:`claim_individually`）：
       实测同一次运行的 3 封未读邮件逐封发全部成功，说明"某一封的问题"
       不该让另外几封陪葬。降级只在**批量已经明确失败**后发生，
       不是重试同一个请求（服务端是原子拒绝 —— 没发奖才回非 0）。
    """

    name = "mail"

    # ------------------------------------------------------------------
    # ① 拉邮件列表
    # ------------------------------------------------------------------
    def fetch_mail_list(self, game: GameClient | None = None) -> MailGetListRes:
        """拉取邮件列表（消息号 13001 → 13002）。

        :param game: 已登录的网关客户端。**建议传入**（见下方说明）；
            省略时会临时新建一个客户端，用完立即关闭。
        :return: 解析好的 :class:`MailGetListRes`。
        :raises client.exceptions.NetworkError: 网络失败时。
        :raises models.envelope.EnvelopeError: 响应结构不符时。

        【为什么 ``game`` 可以省略，但推荐传】
        省略时本方法会自己开一个 :class:`GameClient`：这对"我只想单独调一下
        这个方法"很方便，代价是**每次调用都会重新握手一次**（多一个请求），
        而且新建的客户端不共享上一次拿到的配置指纹 / 令牌。
        在 :meth:`execute` 里两个动作共用同一个客户端，就只握手一次。
        """
        with self._borrow(game) as client:
            view = client.send(
                MailGetListPacket(),
                expect=None,  # 见下方说明：空子包要自己处理，所以不让 send 先解析
                include_defaults=True,
            )
            return self._decode_list(view)

    @staticmethod
    def _decode_list(view: EnvelopeView) -> MailGetListRes:
        """把 13002 的子包解析成 :class:`MailGetListRes`。

        【为什么要单独处理"子包为空"】
        ``decode_sub_packet`` 对空子包一律抛 ``EnvelopeError``（"响应不含业务子包"）——
        这在别的接口上是对的（那里空包确实是异常），但**邮箱为空时服务端就是这样回的**：
        一个长度为 0 的 13002 槽位。若照抛，任务会在"今天没有邮件"时天天报失败，
        而那是完全正常的状态。

        所以这里只对"子包恰好为空"这一种情况返回空结果，**其它解析错误照旧往上抛** ——
        宽容的范围必须窄，否则"字段对齐错了"会被伪装成"没有邮件"。
        """
        try:
            return view.decode_sub_packet(MailGetListRes)  # type: ignore[return-value]
        except EnvelopeError:
            if view.sub_packet_bytes:
                raise
            return MailGetListRes()

    # ------------------------------------------------------------------
    # ② 一键领取
    # ------------------------------------------------------------------
    def claim_all_mails(
        self,
        mail_id_list: Sequence[int],
        game: GameClient | None = None,
    ) -> list[GetObjClass]:
        """一键领取给定的邮件（消息号 13003 → 13004）。

        :param mail_id_list: 邮件 ID 列表 —— 必须来自
            :meth:`MailGetListRes.mail_ids`（服务端下发的 ID，自己编无效）。
        :param game: 同 :meth:`fetch_mail_list`，建议复用同一个客户端。
        :return: **奖励清单**（``MailOpenParcelRes.addList``）——
            每项可能包含道具 / 装备 / 角色等多种内容，本项目只解读其中的道具。
        :raises client.exceptions.ApiError: 服务端返回了非 0 的 ``errorCode`` 时。

        【空列表为什么直接返回，而不是照样发一个空包】
        空包过去只会让服务端做一次无意义的处理；更要紧的是，
        它会掩盖调用方的逻辑错误（"本来以为有 5 封邮件，结果是 0 封"）——
        让它**明显地什么都不做**，比让它悄悄发一个空请求好排查。
        """
        ids = [int(mail_id) for mail_id in mail_id_list]
        if not ids:
            return []

        with self._borrow(game) as client:
            res = self._open_parcel(client, ids)

        if not res.is_success():
            # 业务失败要**抛出去**，不能返回空清单：否则任务会报"成功但没奖励"，
            # 而那是最糟的失败方式（见 tasks/gift_package.py 的同类说明）。
            #
            # 文案刻意**不猜原因**：``-2`` 的语义本项目尚未定性（Q-023），
            # 而"ID 已失效 / 已领取 / 已过期"只是猜测 —— 写进用户可见的错误里，
            # 会把人引向错误方向。真实用法是：调用方（execute）收到本异常后
            # 降级到 :meth:`claim_individually`，由逐封结果告诉我们**到底是哪一封**。
            raise ApiError(
                res.errorCode,
                f"批量领取 {len(ids)} 封邮件被服务端拒绝（errorCode={res.errorCode}）",
            )
        return list(res.addList)

    # ------------------------------------------------------------------
    # ②-2 逐封领取（批量被服务端拒绝后的降级路径）
    # ------------------------------------------------------------------
    def claim_individually(
        self,
        mail_id_list: Sequence[int],
        game: GameClient | None = None,
    ) -> SingleClaimOutcome:
        """**逐封**发 13003：成功的收下、失败的记账（自己不抛异常）。

        :param mail_id_list: 要尝试的邮件 ID（通常来自
            :meth:`models.mail.MailGetListRes.unread_mail_ids`）。
        :param game: 复用调用方的客户端（理由同 :meth:`claim_all_mails`：少握一次手）。
        :return: :class:`SingleClaimOutcome`（成功封数 / 奖励 / 失败清单）。

        【什么时候会走到这里】
        :meth:`execute` 先按协议本意**一次批量**发；只有整包被拒（``errorCode != 0``）
        时才降级到本方法。于是：

        * 正常情况仍是 1 个请求（协议设计的"一键"高效路径），
        * 异常情况也不会"一封都没领到"。

        【为什么这里不抛异常、也不对同一封重试】
        * 抛异常会让"3 封成功、2 封失败"被整体报成失败 —— 那 3 封的奖励**已经到账**，
          说成失败既误导人，又会让上层以为可以整批重跑；
        * 每封**最多发一次**：实测已读邮件必回 ``-2``，重复发不会变成成功，
          只会多一次风控曝光。

        【为什么连 ``EnvelopeError`` 也一起捕获】
        单封响应结构异常（例如 13004 子包为空）只该毁掉那一封，
        不该让其余几封连试的机会都没有。
        """
        ids = [int(mail_id) for mail_id in mail_id_list]
        outcome = SingleClaimOutcome()
        if not ids:
            return outcome

        with self._borrow(game) as client:
            for index, mail_id in enumerate(ids):
                if index:
                    # 见 SINGLE_CLAIM_INTERVAL_SECONDS：宁可慢 1 秒，也别让 5 个包
                    # 在同一毫秒里砸到网关上。
                    time.sleep(SINGLE_CLAIM_INTERVAL_SECONDS)
                try:
                    res = self._open_parcel(client, [mail_id])
                except (CherrytaleError, EnvelopeError) as exc:
                    # debug 级：裸 mailID 是**开发者日志**的内容，不进运行视图
                    # （汇总消息里已有失败封数）—— 见本模块「结果视图纪律」。
                    self.logger.debug("邮件 %s 逐封领取失败（网络/协议层）：%s", mail_id, exc)
                    outcome.failed.append((mail_id, None))
                    continue

                if res.is_success():
                    outcome.claimed += 1
                    outcome.rewards.extend(res.addList)
                    self.logger.debug("邮件 %s 领取成功：%s", mail_id, res.reward_summary())
                else:
                    outcome.failed.append((mail_id, res.errorCode))
                    self.logger.debug(
                        "邮件 %s 领取被拒：errorCode=%s（已读 / 已领过的邮件属正常）",
                        mail_id,
                        res.errorCode,
                    )

        return outcome

    # ------------------------------------------------------------------
    # ③ 内部工具
    # ------------------------------------------------------------------
    def _open_parcel(
        self, game: GameClient, mail_id_list: Sequence[int]
    ) -> MailOpenParcelRes:
        """真正组装并发送 13003，返回原始响应（失败判断交给调用方）。"""
        packet = MailOpenParcelPacket.for_ids(mail_id_list)
        view = game.send(packet, expect=MailOpenParcelRes, include_defaults=True)
        return view.decode_sub_packet(MailOpenParcelRes)  # type: ignore[return-value]

    def _borrow(self, game: GameClient | None) -> AbstractContextManager[GameClient]:
        """借用传入的客户端；没传就新建一个（离开 ``with`` 时自动关闭）。

        【为什么需要这个小工具】
        ``GameClient`` 是上下文管理器（用它才能保证连接被关掉），
        但"调用方已经给了客户端"时**不能**把别人的连接关掉。
        ``nullcontext`` 正好表达"什么都不做的 with"，
        于是调用点可以统一写成 ``with self._borrow(game) as client:``，
        而不必在两个分支里各写一遍发包逻辑。
        """
        if game is not None:
            return nullcontext(game)
        return self.open_game_client()

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """拉列表 → 筛出**未读** → 一次批量领取（被拒则逐封降级）→ 汇总。

        :raises client.exceptions.CherrytaleError: 网络 / 业务失败时
            （由 :meth:`tasks.base.BaseTask.run` 转成失败结果）。

        【流程为什么是这样（每一步都有真机依据）】

        1. 13001 → 13002 拿列表；
        2. **只把未读邮件**交给 13003 —— 混进已读的会让整包回 ``-2``
           （实证见 :meth:`models.mail.MailGetListRes.unread_mail_ids`）；
        3. 先按协议本意发**一个**批量请求（"一键领取"就该是一个请求）；
        4. 批量被整体拒绝时，才逐封重发（:meth:`claim_individually`），
           把"某一封领不了"限制在一封之内；
        5. 部分成功也照实报告成功/失败**封数** —— 但**不报领到了哪些邮件**
           （口径纪律见模块文档；哪几封失败看 DEBUG 日志）。

        这里刻意**把同一个 ``game`` 传给两个动作**：握手一次就够，
        而且 13002 响应可能带回新的配置指纹，13003 顺带就带上了。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            res = self.fetch_mail_list(game)
            all_ids = res.mail_ids()
            unread_ids = res.unread_mail_ids()
            read_count = res.read_mail_count()

            self.logger.info(
                "邮件共 %d 封：未读 %d 封（待领）、已读 %d 封（跳过）",
                len(res.mailList),
                len(unread_ids),
                read_count,
            )
            # 具体是哪些 mailID 属于**开发者日志**内容 —— 运行视图只报封数。
            self.logger.debug("待领 mailID=%s", unread_ids or "无")
            if len(all_ids) != len(res.mailList):
                # 不抛错：少数几封取不到 ID 不该让整个任务失败，但必须让你看见。
                self.logger.warning(
                    "有 %d 封邮件没解析出 mailID（字段 1）—— 若长期出现，"
                    "请核对 models/mail.py 里 MAIL_ID_FIELD 的取值",
                    len(res.mailList) - len(all_ids),
                )

            if not unread_ids:
                # 空邮箱与"全是已读"要分开说：后者通常是**都领过了**，
                # 报成"领取失败"会让用户以为工具坏了。
                if res.mailList:
                    message = (
                        f"邮箱里没有未读邮件可领取（共 {len(res.mailList)} 封，"
                        f"其中已读 {read_count} 封）—— 已读的通常是已经领过的，"
                        "服务端会拒绝重复领取，属正常状态"
                    )
                else:
                    message = "邮箱里没有可领取的邮件"
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=message,
                    data={
                        "mails": len(res.mailList),
                        "claimed": 0,
                        "read": read_count,
                    },
                )

            claimed = 0
            failed: list[tuple[int, int | None]] = []
            try:
                # 返回值（13004 的 addList）刻意**不接收**：它的 itemAmount 是
                # "领取后持有量"，不能当"获得"展示，本任务也不再展示邮件明细
                # —— 详见模块文档的「结果视图纪律」。
                self.claim_all_mails(unread_ids, game)
                claimed = len(unread_ids)
            except ApiError as exc:
                # 批量被**整体**拒绝 —— 实测：清单里混进不可领的邮件（例如已读的）
                # 就是这个结果，而且服务端一封都不会发。
                # 逐封重来：把"一封的问题"限制在一封之内，别让其余几封陪葬。
                self.logger.warning(
                    "%s → 改为逐封领取 %d 封（每封间隔 %.1fs）",
                    exc,
                    len(unread_ids),
                    SINGLE_CLAIM_INTERVAL_SECONDS,
                )
                outcome = self.claim_individually(unread_ids, game)
                if outcome.claimed == 0:
                    # 一封都没成功：这才是真正的"整体失败"，如实抛给基类处理。
                    raise ApiError(
                        exc.code,
                        f"逐封领取 {len(unread_ids)} 封邮件全部失败（errorCode={exc.code}）",
                    ) from exc
                claimed = outcome.claimed
                failed = outcome.failed

        if failed:
            # 部分成功也必须如实报告成功/失败**封数**。
            # 哪几封失败（裸 mailID）留给开发者日志 —— 运行视图不出现邮件明细。
            self.logger.debug("逐封领取失败清单（mailID, errorCode）：%s", list(failed))
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"逐封领取：成功 {claimed} 封、失败 {len(failed)} 封"
                    "（多为已读 / 已领过）"
                ),
                data={
                    "mails": len(res.mailList),
                    "claimed": claimed,
                    "failed": len(failed),
                },
            )
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"一键领取 {claimed} 封邮件成功",
            data={
                "mails": len(res.mailList),
                "claimed": claimed,
            },
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉邮件列表"这一包，不发任何领取请求。"""
        request = game.build(MailGetListPacket(), include_defaults=True)
        print(request.describe())
        print()
        print("真实邮件 ID 要等 13002 响应才能确定（本演练不联网）：")
        print("  · 13001 拉列表 → 从每封邮件里取 mailID（字段 1）→ 13003 一次性领取")
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：13001 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )
