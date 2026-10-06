"""角色（英雄）只读任务：**5011 实时名册 + 登录快照兜底**。

【这个任务解决什么】
网页「角色与资源」面板要显示"我有哪些角色、各多少级"。这份数据有两个来源，
**可靠性不同**，所以两条都走：

============================  =========  ==========================================
来源                           包号        特点
============================  =========  ==========================================
``LoginRes.roleInitClass``    1004       **登录那一刻**一次性下发；只在 ``login``
                                         任务跑过、且会话文件里存了快照时才有。
                                         结构可信（字段表完整），但**会过时**。
``GetAllRoleRes.roleList``    5012       5011 的响应，**实时**。但 5012 从未在
                                         任何抓包里出现过（字段形状由 dump.cs
                                         推定），真机是否可用**尚未验证**。
============================  =========  ==========================================

所以顺序是：**先试 5011 拿实时**；拿不到（空 / 报错）就**退回登录快照**，
并在结果里**明确写出用的是哪一份**。绝不把两种来源混在一起报 ——
"这份数据是刚拉的还是登录时的"是用户判断"数字对不对"的关键前提。

【为什么必须有兜底】
本工具的每条命令都是新进程：``python main.py run daily`` 复用 ``.session.json``
里的令牌，**不会重新登录**，因此那时根本没有 1004。没有快照兜底的话，
"角色查看"在这条最常见的路径上就是空的。

【只读承诺】
白名单只有 ``5011 GetAllRolePacket``（空报文）；``models/role.py`` 的
``FORBIDDEN_PACKET_IDS`` 把升级 / 转移 / 超限 / 训练 / 换皮肤全部列进禁发清单，
越界会在运行期立刻报错。本任务**不写任何持久化状态**（不落盘、不改会话）——
快照的刷新时机是"登录"，不是"看一眼角色"。
"""

from __future__ import annotations

import logging
from typing import Any

from client.exceptions import CherrytaleError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.game_config import RoleIdMap, load_role_id_map
from models.role import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GetAllRolePacket,
    GetAllRoleRes,
)
from models.session_state import role_snapshot_from
from services.avatar_service import resolve_role_icon_code
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

#: 结果里标记"数据来自实时名册"
SOURCE_LIVE: str = "live"
#: 结果里标记"数据来自登录快照"
SOURCE_SNAPSHOT: str = "snapshot"

_SOURCE_LABEL: dict[str, str] = {
    SOURCE_LIVE: "实时名册（5012）",
    SOURCE_SNAPSHOT: "登录快照（1004，可能已过时）",
}


@register_task
class RolesTask(BaseTask):
    """角色（英雄）只读查询。零消耗：只发一个空包。"""

    name = "roles"

    #: 要带令牌与 playerId 才能发包
    requires_auth = True

    #: 白名单里只有一个只读空包，不存在花钱路径
    spends_diamond = False

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        #: 本次实际发出的消息号（测试用它断言"没有越界发包"）
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # 数据来源
    # ------------------------------------------------------------------
    def _fetch_live(self, game: GameClient) -> GetAllRoleRes:
        """发 5011 拉实时名册（唯一的只读包）。"""
        view = guarded_send(
            game,
            GetAllRolePacket(),
            GetAllRoleRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(GetAllRoleRes)

    def _snapshot(self) -> tuple[dict[str, int], ...]:
        """取登录时存下的角色快照（没有会话状态时返回空元组）。"""
        state = self.session.game_state
        if state is None:
            return ()
        return tuple(state.roles)

    def _load_role_ids(self) -> RoleIdMap | None:
        """读角色 ID 换算表（拿中文名与 RoleInfoID 用）。

        :return: 换算表；读不到返回 ``None`` —— **不阻断任务**：
            角色列表本身（sid / RoleMainID / 等级 / 星级）不依赖配置表，
            只是少个中文名。为了一张展示用的表把整个功能停掉不划算。
        """
        try:
            return load_role_id_map()
        except (OSError, ValueError) as exc:
            self.logger.warning(
                "读不到角色配置表（RoleMainData / RoleInfoData），本次只报 ID 不报名字：%s",
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """先试实时名册，拿不到就退回登录快照，并说明用的是哪一份。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            snapshot: tuple[dict[str, int], ...] = ()
            source = SOURCE_SNAPSHOT
            fallback_reason = ""
            try:
                live = self._fetch_live(game)
            except CherrytaleError as exc:
                # 5011 被拒（结构/权限/网络）—— 不是致命错误：还有快照可用。
                # 但必须**留痕**：否则"实时那份坏了"这件事会被静默掩盖，
                # 用户只会奇怪"为什么数据看着是旧的"。
                fallback_reason = f"实时名册请求失败（{exc}）"
                self.logger.warning("5011 拉取失败，改用登录快照：%s", exc)
            else:
                if live.roleList:
                    snapshot = role_snapshot_from(live.roleList)
                    source = SOURCE_LIVE
                else:
                    fallback_reason = "服务端回了空的角色列表（5012 roleList 为空）"
                    self.logger.warning("%s，改用登录快照", fallback_reason)

            if source == SOURCE_SNAPSHOT:
                snapshot = self._snapshot()

            if not snapshot:
                # 两条路都空 → 如实说清"没有数据"，并给出可执行的下一步。
                message = (
                    "没有拿到角色数据："
                    f"{fallback_reason or '实时名册为空'}，且会话里没有登录快照。"
                    "跑一次 `python main.py run login` 会把 1004 的角色名册存下来"
                )
                self.logger.warning("%s", message)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=message,
                    data={
                        "source": source,
                        "fallback_reason": fallback_reason,
                        "role_count": 0,
                        "roles": (),
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

        roles = self._describe(snapshot, self._load_role_ids())
        label = _SOURCE_LABEL[source]
        message = f"角色 {len(roles)} 个（来源：{label}）"
        if fallback_reason:
            message += f"；{fallback_reason}"
        self.logger.info("%s", message)
        return TaskResult(
            task=self.name,
            ok=True,
            message=message,
            data={
                "source": source,
                "fallback_reason": fallback_reason,
                "role_count": len(roles),
                "roles": roles,
                "sent_packets": tuple(self.sent_packet_ids),
            },
        )

    def _describe(
        self,
        snapshot: tuple[dict[str, int], ...],
        role_ids: RoleIdMap | None,
    ) -> tuple[dict[str, Any], ...]:
        """把快照补上"配置表侧"的信息（中文名、RoleInfoID、头像），按等级降序输出。

        :param snapshot: :func:`models.session_state.role_snapshot_from` 的产物。
        :param role_ids: 换算表；``None`` 时名字留空、``role_info_id`` / ``icon_code`` 记空。
        :return: 可直接 JSON 序列化的列表。

        .. note::
           ``role_info_id`` 是**配置表推出来的**（``roleID`` 沿品质升级链回溯
           再反查），不是协议字段 —— 这里算出来是为了让界面能回答
           "这个角色能不能去占那个矿点"，而不是把它当成新的真值来源。

        .. note::
           ``icon_code`` 是**静态头像目录的文件名**（如 ``a001_03h``），前端直接拼
           ``assets/avatars/{icon_code}.png``。它是**查文件存在性换来的**，不是协议
           字段；查不到时给空串，让前端回退统一占位图 —— 这里**绝不给假路径**
           （见 ``services/avatar_service.py::resolve_role_icon_code``）。
        """
        described: list[dict[str, Any]] = []
        for entry in snapshot:
            main_id = int(entry.get("roleID", 0))
            info_ids = role_ids.role_info_ids(main_id) if role_ids else ()
            info_id = info_ids[0] if info_ids else 0
            described.append(
                {
                    "role_sid": int(entry.get("roleSid", 0)),
                    "role_id": main_id,
                    "role_info_id": info_id,
                    "level": int(entry.get("roleLV", 0)),
                    "star": int(entry.get("roleStar", 0)),
                    "name": role_ids.name_of(main_id) if role_ids else "",
                    "icon_code": resolve_role_icon_code(main_id) if role_ids else "",
                }
            )
        # 等级降序（同级按星级、再按 sid）—— 与挖矿挑角色的"从高到低"口径一致，
        # 界面上第一眼看到的就是最能打的。
        described.sort(
            key=lambda item: (item["level"], item["star"], item["role_sid"]),
            reverse=True,
        )
        return tuple(described)

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装并打印将要发送的字节，**不联网**。"""
        request = game.build(GetAllRolePacket())
        print(f"将要发送：{PACKET_PURPOSE[5011]}")
        print(request.describe())
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：5011 请求体 {len(request.body)} 字节，未发送",
            data={"planned_packets": (5011,), "sent_packets": ()},
        )
