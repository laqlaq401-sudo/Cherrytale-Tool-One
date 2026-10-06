"""工会签到（消息号 21055 拉签到面板 → 21057 签到 → 21061 领连线奖励）。

【流程（四段式）】
1. **拉面板**：``AllianceBingoHomePacket``（21055，空包）→ ``bingoData``
   是**已被占**格子的清单；
2. **挑格子**：★ 2026-10-03 按抓包重写 —— ``bingoData`` 只含已占格子
   （未签的根本不在响应里），旧实现"取 iconData 为空的格子"永远为空、
   任务从未真正签过。现在取 ``max(已占 pos) + 1``（空面板取 0），
   与真实客户端"自由选一个未占格子"的实测行为一致（10.3 抓包：已占
   {0..7,13,19} 时客户端签 pos=25，服务端接受）；
3. **签到**：``AllianceBingoSignInPacket{pos}``（21057），**每日一次、不重试**；
4. **领连线奖励 + 汇总**：★ 新增 21061 ``AllianceBingoRewardPacket``（空包）
   —— 10.3 抓包证明签到后立即领即可拿到连线奖励（旧决策"等次日转邮箱"
   与实测不符），然后如实报告签没签上、获得了什么。

【"没入会"不算失败；"今日已签"由服务端裁决】
* 21055 回 ``errorCode=19``（``HaveNotBeen_ExistedMyAlliance``）→ 没加入工会，
  任务无可做，``ok=True`` 并说明；
* 本地无法判断"今天是否已签"（棋盘全集没有静态来源），重跑时若服务端
  对 21057 回错误码，会原样展示 —— 漏报错误才是错误。

【消息号与字段依据】
``models/alliance.py``（21055/21056/21057/21058/21061/21062 的模型与白名单表），
字段编号见 ``models/proto_fields.py``；取证见 ``notes/dossier/AllianceBingo*.json``
与 ``captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz``。
"""

from __future__ import annotations

from typing import ClassVar

from client.game_client import GameClient
from models.alliance import (
    FORBIDDEN_PACKET_IDS,
    SIGN_IN_ALLOWED_PACKET_IDS,
    AllianceBingoHomePacket,
    AllianceBingoHomeRes,
    AllianceBingoRewardPacket,
    AllianceBingoRewardRes,
    AllianceBingoSignInPacket,
    AllianceBingoSignInRes,
    ERR_NOT_IN_ALLIANCE,
    is_not_in_alliance,
    error_code_name,
)
from tasks.base import BaseTask, QuantityScope, TaskResult, register_task, reward_phrase
from tasks.packet_guard import guarded_send


@register_task
class AllianceSignInTask(BaseTask):
    """工会签到：在 Bingo 面板的空格子上自动签到（每日一次）。"""

    name = "alliance_sign_in"

    requires_auth = True

    #: 纯领取：不消耗任何资源，也不存在任何花钱分支
    spends_diamond = False

    opt_in_hint: ClassVar[str] = "已加入工会才会执行；每日一次，零消耗"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # ① 拉签到面板
    # ------------------------------------------------------------------
    def _fetch_bingo(self, game: GameClient) -> AllianceBingoHomeRes:
        """拉取签到面板（21055 → 21056，只读）。"""
        view = guarded_send(
            game,
            AllianceBingoHomePacket(),
            AllianceBingoHomeRes,
            allowed=SIGN_IN_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        return view.decode_sub_packet(AllianceBingoHomeRes)

    # ------------------------------------------------------------------
    # ③ 签到
    # ------------------------------------------------------------------
    def _sign(self, game: GameClient, pos: int) -> tuple[bool, str, AllianceBingoSignInRes]:
        """在指定格子上签到。

        :return: ``(是否成功, 可读结果, 原始响应)``。

        .. warning::
           **不重试**。签到每日一次，超时后服务端可能已经记成功，
           重试只会拿到"已签过"一类的错误。
        """
        # ★ include_defaults=True：pos=0（首个格子）时该字段会被省略 →
        # 服务端字节校验可能拒收（Q-050 同源，2026-10-04 预防性补上）。
        view = guarded_send(
            game,
            AllianceBingoSignInPacket.at_position(pos),
            AllianceBingoSignInRes,
            allowed=SIGN_IN_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
            include_defaults=True,
        )
        res = view.decode_sub_packet(AllianceBingoSignInRes)
        if not res.is_success():
            return False, f"errorCode={res.errorCode}（{error_code_name(res.errorCode)}）", res
        return True, f"已签在 {pos} 号格子", res

    # ------------------------------------------------------------------
    # ③.5 领连线奖励（★ 2026-10-03 新增，21061）
    # ------------------------------------------------------------------
    def _claim_bingo_reward(
        self, game: GameClient
    ) -> tuple[bool, str]:
        """领取 Bingo 连线奖励（21061 空包 → 21062）。

        :return: ``(是否成功, 可读结果)``。

        .. note::
           **不重试**：连线奖励一次性领取；若今天没有可领的连线，
           服务端会回错误码，这里如实转述，不猜测。

        .. warning::
           这一步失败**不推翻**签到本身的成败 —— 签到已生效，奖励没领到
           只是少一笔进账，两者在结果里分开呈现。
        """
        view = guarded_send(
            game,
            AllianceBingoRewardPacket(),
            AllianceBingoRewardRes,
            allowed=SIGN_IN_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        res = view.decode_sub_packet(AllianceBingoRewardRes)
        if res.errorCode == -32:
            # ★ 2026-10-04 真机实测：签到成功但服务端回 -32 —— 当天没有可领的
            # 连线（还没连成线，或该线的奖励已领过）。这不是失败：签到已生效，
            # 奖励本来就不存在，按"跳过"处理，不算进失败。
            return True, "连线奖励暂无可领（errorCode=-32：尚未连成线或已领过）"
        if not res.is_success():
            return False, f"errorCode={res.errorCode}（{error_code_name(res.errorCode)}）"
        # 21062 的数量口径尚未实证 → 保守口径只报件数
        # （见 models/reward.py 的 QuantityScope.UNKNOWN）。
        text = reward_phrase(res.getObjClass, scope=QuantityScope.UNKNOWN)
        return True, f"连线奖励：{text}" if text else "连线奖励：无道具明细"

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """拉面板 → 推断位置 → 签到 → 领连线奖励，如实汇总。

        :raises client.exceptions.CherrytaleError: 网络失败时（由基类转成失败结果）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            bingo = self._fetch_bingo(game)
            if not bingo.is_success():
                if is_not_in_alliance(bingo.errorCode):
                    message = "未加入工会，跳过签到"
                    self.logger.info("%s", message)
                    return TaskResult(task=self.name, ok=True, message=message, data={"skipped": True})
                message = f"拉取签到面板失败：errorCode={bingo.errorCode}（{error_code_name(bingo.errorCode)}）"
                self.logger.warning("%s", message)
                return TaskResult(task=self.name, ok=False, message=message)

            pos = bingo.next_sign_position()
            if pos is None:
                # next_sign_position 只在无法推断时返回 None（当前实现不会走到，
                # 防御性保留：宁可如实说"不知道签哪"，也不猜一个编号）
                message = "无法从面板推断可签的格子位置，放弃发送"
                self.logger.warning("%s", message)
                return TaskResult(task=self.name, ok=False, message=message, data={"pos": None})

            ok, text, res = self._sign(game, pos)
            if not ok:
                message = f"工会签到失败（格子 {pos}）：{text}"
                self.logger.warning("%s", message)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=message,
                    data={"pos": pos, "error_code": res.errorCode},
                )

            rewards = self._reward_text(res)

            # ⑤ 连线奖励：签到已生效，这一步的成败单独呈现，不并进签到的成败
            reward_ok, reward_text_line = self._claim_bingo_reward(game)
            if not reward_ok:
                self.logger.warning("✘ 连线奖励领取失败：%s", reward_text_line)

            message = f"工会签到成功：{text}{rewards}"
            if reward_ok:
                message += f"；{reward_text_line}"
            else:
                message += f"；连线奖励未领到（{reward_text_line}）"
            self.logger.info("✔ %s", message)
            return TaskResult(
                task=self.name,
                ok=True,
                message=message,
                data={
                    "pos": pos,
                    "signed": True,
                    "bingo_reward": reward_text_line if reward_ok else None,
                },
            )

    @staticmethod
    def _reward_text(res: AllianceBingoSignInRes) -> str:
        """把签到直接获得的道具汇总成一行（口径未实证 → 只报件数，没有则返回空串）。"""
        phrase = reward_phrase(res.getObjClass, scope=QuantityScope.UNKNOWN)
        return f"，{phrase}" if phrase else ""

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉面板"、"签到"、"领连线奖励"三包，说明将挑哪个格子。"""
        home_request = game.build(AllianceBingoHomePacket())
        print(home_request.describe())
        print()
        print(
            "签到请求要等 21055 响应才能确定 pos（将取 max(已占 pos)+1，"
            "空面板取 0 —— 见 AllianceBingoHomeRes.next_sign_position）；"
            "下面是 pos=0 时的样子（仅示意字段编号）："
        )
        sign_request = game.build(AllianceBingoSignInPacket.at_position(0))
        print(sign_request.describe())
        reward_request = game.build(AllianceBingoRewardPacket())
        print(reward_request.describe())
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：21055 请求体 {len(home_request.body)} 字节，未发送",
            data={"body_size": len(home_request.body)},
        )
