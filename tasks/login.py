"""登录任务（**区服选择已实现；发包部分仍未落地**）。

【这条链路整体长什么样（dump.cs + 抓包实测）】

    99015 / 99016  握手        → 拿网关配置（serverIP / serverPort / cdnList / dnsList…）
    1001  / 1002   账号登录    → 响应里带 **区服列表**（allServerList / suggestedServer）
    1003  / 1004   进区登录    → ``LoginPacket.serverID`` = 上一步选中的**区服编号**
    1010           LoginOtherDataPacketRes → 进游戏后的补充数据（344 KB 的那条）

【★ 1004 不只是"拿令牌"（2026-10-05）】
``LoginRes`` 同时下发玩家的**全部家当快照**：装备(4) / **角色名册(5)** / 背包(6) /
队伍(7)。客户端登录后**不会**再去单独拉这些（实测登录抓包里没有 5011，
打开英雄界面也一条网关请求都没有）。所以本任务顺手把角色名册压成快照
存进会话状态（见 ``models.session_state.role_snapshot_from``），
供「角色查看」与 ``roles`` 任务离线兜底 —— 那是 1004 唯一能拿到的时刻。

所以「选服」不是可选项：1002 只给你一张列表，**必须**把其中一个 ``serverID``
回填进 1003，才会拿到 1004 LoginRes。

【✅ 已完成：区服选择（本文件 + ``models/server.py``）】
1. 能解析 1002 响应：``models/server.py`` 的 ``AccountClientInfoLoginRes``
   （含 ``ServerListClass`` 的 7 个字段，编号已被真实字节验证）；
2. 能挑区服：优先级 = 显式配置 → 服务端建议 → 上次登录 → 列表第一个
   （见 :func:`choose_server_for_login`）；
3. 能生成带 ``serverID`` 的登录包：``LoginPacket.for_server()``；
4. **可离线验证**（不需要账号、不发任何请求）::

       python tools/show_servers.py --write notes/server_list.md
       python tools/show_servers.py --server-id 3
       python -m pytest tests/test_server.py -q

【✅ 已完成：真正的发包（1001 → 1002 → 选服 → 1003 → 1004）】
全链路已实测跑通（拿到 ``playerId`` 与 34 字符会话令牌），并且**令牌有了正规出口**：

1. 平台身份 → ``userId``（``client/platform_client.py``）；
2. 1001 → 1002：拿账号态与区服列表；
3. 选服 → 1003 → 1004：令牌在子包 ``playerInitClass.token``（**不在信封元信息里**）；
4. 令牌/playerId/区服被装进 ``models/session_state.py`` 的
   ``GameSessionState``，然后做两件事：
   **写进 ``self.session.set_game_state(...)``**（同进程共享，``tasks/daily.py`` 直接用）
   与 **写入会话文件**（跨进程共享，``python main.py run daily`` 自动复用）。

原文保留在这里，是为了记住"当初为什么卡住"（HAR 的请求体是有损文本、
不能靠试探性发包去猜参数），而**不是**因为还没做：

【⏳ 当时的阻塞点（现已解决）】
- 请求体取值没在真实字节上核对过 → 后来用 Fiddler 的 HexView 拿到完整 hex；
- vCode 算法未知 → 已逆向并落地在 ``crypto/vcode.py``（两个样本双向命中）；
- 1003 被拒的真正原因 → 是 vCode 与 ``hamiSubNoList``，不是区服/字段编号/请求头。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Final

import config
from client.account_store import AccountStore, SavedAccount
from client.credentials import describe_source, resolve_credentials
from client.exceptions import ApiError, AuthenticationError, CherrytaleError
from client.game_client import GameClient
from client.platform_client import PlatformClient
from client.session_store import save_session_state, session_file_path
from models.envelope import EnvelopeError
from models.handshake import SpecialActivityRefreshClass
from models.server import (
    AccountClientInfoLoginPacket,
    AccountClientInfoLoginRes,
    LoginRes,
    LoginPacket,
    ServerListClass,
    ServerSelectError,
)
from models.session_state import GameSessionState, role_snapshot_from
from services.avatar_service import resolve_avatar_png
from tasks.base import BaseTask, TaskResult, register_task
from tasks.login_signin import run_if_first_login_today

#: 游戏网关的请求路径。**实测就是根路径 ``/``**（抓包里 POST 的目标是
#: ``https://game-ct-labs.ecchi.xxx:1893/``），不是常见的 ``/api/xxx``。
LOGIN_PATH: str = "/"

#: 演练模式（``--dry-run``）用的占位 userId。
#:
#: 长度与真实 userId 一致（38 字符），这样演练输出的**字节长度与真实发包一致** ——
#: 否则"演练看着对、真发时长度不同"会白高兴一场。
_DRY_RUN_USER_ID: Final[str] = "ER00000000-0000-0000-0000-000000000000"

#: 本模块的日志器。
#: ``choose_server_for_login`` 是模块级函数（没有 ``self.logger``），但它有一条
#: **必须留痕**的兜底行为（新账号进推荐新区），所以给它一个模块级 logger。
_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.login")


# ---------------------------------------------------------------------------
# 区服选择（可独立验证的一步）
# ---------------------------------------------------------------------------
def choose_server_for_login(response: AccountClientInfoLoginRes) -> ServerListClass:
    """按 ``config`` 的配置，从 1002 响应里挑出本次要进的区服。

    :param response: 解析好的 ``AccountClientInfoLoginRes``（1002 响应）。
    :raises models.server.ServerSelectError: 配置的区服不存在，或响应里没有可用区服。

    选择优先级由 ``AccountClientInfoLoginRes.choose_server`` 实现：
    ``config.SERVER_ID``（环境变量 ``CHERRYTALE_SERVER_ID``）→ 服务端建议 →
    上次登录 → 列表第一个。这里只负责把配置传进去，不再自己写一套规则 ——
    规则散落两处是"改了 A 忘了 B"的经典来源。

    ⚠️ ``prefer_with_character=True`` 是**防御性**设置：服务端建议的区
    （``suggestedServer``）是**推荐给新玩家的新区**，本账号在那里往往没有角色。
    排查 1003 被拒时曾怀疑过这一点（后来证实真正原因在 vCode 算法上），
    但"进自己玩过的区"更符合跑日常的意图，也能避免在新区误开空号。
    （确实想进新区开小号时，用 ``CHERRYTALE_SERVER_ID`` 显式指定。）

    【新账号的兜底（本函数比 ``choose_server`` 多出来的一层）】
    如果本账号在**所有区都没有角色**（全新账号就是这种情况），上面的过滤会让候选为空、
    直接抛 ``ServerSelectError``。此时本函数会**退一步用服务端建议的区**，并打一条
    WARNING 说明"会在该区创建角色"。

    三条边界（保证这不是"悄悄帮你换一个"）：

    1. 只在**没有显式指定区服**时兜底；``CHERRYTALE_SERVER_ID`` 指定了却没命中 → 原样报错；
    2. 兜底一定**留痕**（WARNING 里带上区服标签与"如何改成你想要的区"）；
    3. 兜底也失败时（例如分组过滤把候选全清空）→ 抛出**更具体的原始原因**，不吞掉信息。
    """
    try:
        return response.choose_server(
            server_id=config.SERVER_ID,
            group_id=config.SERVER_GROUP_ID,
            prefer_with_character=True,
        )
    except ServerSelectError as original:
        # 显式指定了区服却选不出来 → 那是配置问题，必须让人看到，绝不兜底。
        if config.SERVER_ID is not None:
            raise
        try:
            fallback = response.choose_server(
                server_id=None,
                group_id=config.SERVER_GROUP_ID,
                prefer_with_character=False,
            )
        except ServerSelectError:
            # 兜底也失败（例如限定了分组而该组没有区）→ 报更具体的原始原因
            raise original from None
        _LOGGER.warning(
            "本账号在服务端下发的所有区里都**没有角色**，按服务端建议的区继续：%s\n"
            "  ⚠️ 首次进区会在该区**创建角色**。\n"
            "  如果这不是你要的区，用环境变量显式指定，例如："
            "CHERRYTALE_SERVER_ID=%d",
            fallback.label(),
            fallback.serverID,
        )
        return fallback

def build_login_packet(
    response: AccountClientInfoLoginRes,
    server: ServerListClass | None = None,
    *,
    platform_user_id: str = "",
) -> LoginPacket:
    """生成进区登录包（1003）：把选中的 ``serverID`` 填进去。

    :param response: 1002 响应（``accountID`` 要从这里取）。
    :param server: 已选定的区服；``None`` 表示按 :func:`choose_server_for_login` 现挑。
    :param platform_user_id: **平台侧 userId**，会填进 ``hamiSubNoList``。
        ⚠️ 这个参数**不能省**：实测不带它时服务端一律回 ``ABNORMAL_PACKET``
        （服务端靠它定位账号）。详见 ``LoginPacket.for_server``。
    """
    chosen = choose_server_for_login(response) if server is None else server
    return LoginPacket.for_server(
        chosen,
        account_id=response.accountID,
        platform_user_id=platform_user_id,
        channel_platform_type=config.CHANNEL_PLATFORM_TYPE_EL,
        client_version=config.GAME_CLIENT_VERSION,
        language_code=config.GAME_LANGUAGE,
    )



@register_task
class LoginTask(BaseTask):
    """登录并获取会话令牌。"""

    name = "login"

    #: 登录本身当然不需要先持有令牌
    requires_auth = False

    def __init__(
        self,
        session: Any,
        *,
        account: str | None = None,
        server_id: int | None = None,
        plan_only: bool = False,
        **kwargs: Any,
    ) -> None:
        """构造登录任务。

        :param session: 任务会话（见 ``BaseTask``）。
        :param account: **账号选择器**（序号 ``1`` / 邮箱 / userId），来自命令行
            ``--account``；``None`` 表示按 :meth:`_resolve_platform_user_id` 的
            优先级自动决定（也会读环境变量 ``CHERRYTALE_ACCOUNT``）。
        :param server_id: **强制进入的区服编号**；``None`` 表示按
            :func:`choose_server_for_login` 的既有规则挑
            （``config.SERVER_ID`` → 服务端建议 → 上次登录 → 列表第一个）。
            网页/窗口的「选区」界面就是靠它把用户点的那一区传进来的。
        :param plan_only: **只拉区服列表**：走到 1001 拿到 1002 就返回，
            **不写会话、不写账号库、不发 1003**。见 :meth:`_plan_only_result`。
        """
        super().__init__(session, **kwargs)
        self.account = account
        #: 显式指定的区服（``None`` = 按规则挑）
        self.server_id = server_id
        #: 是否"只拉区服列表就返回"（登录 / 选区两段式流程的第一段）
        self.plan_only = plan_only
        #: 本次实际使用的账号库记录（用于进区成功后回写角色信息）
        self._active_account: SavedAccount | None = None

    @property
    def account_selector(self) -> str | None:
        """本次要用的账号选择器：``--account`` 优先，其次环境变量 ``CHERRYTALE_ACCOUNT``。"""
        value = (self.account or os.getenv(config.PLATFORM_ACCOUNT_ENV_NAME, "")).strip()
        return value or None

    def execute(self) -> TaskResult:
        """执行登录：平台令牌 → userId → 1001 → 1002 → 选服 → 1003 → 1004。

        :raises AuthenticationError: 未配置平台令牌时。
        :raises ApiError: 服务端返回业务错误码时。
        :raises models.server.ServerSelectError: 没有可用区服时。
        :raises client.exceptions.NetworkError: 网络失败时。

        【这条链路为什么是这个顺序】
        1001 用**平台 userId** 登录（实测：``socialAccount`` / ``socialOpenId`` 都是它），
        响应 1002 给出**区服列表**；区服编号只能从这里取、客户端算不出来，
        所以必须用 1003 把选中的 ``serverID`` 回填，才会拿到 1004。
        """
        # ① 平台侧拿 userId —— 这是游戏登录唯一的"凭据"
        #
        # 【演练模式为什么不调用平台接口】
        # ``--dry-run`` 的承诺是"不发任何请求"。若这里仍去调平台网关，
        # 演练就变成了"发了一半"：在网络受限或令牌失效时还会莫名失败，
        # 让人误以为是字节组装出了问题。所以演练用占位 userId 离线组装。
        if self.dry_run:
            user_id = _DRY_RUN_USER_ID
            self.logger.info("演练模式：使用占位 userId（不调用平台接口）")
        else:
            user_id = self._resolve_platform_user_id()

        # 会话状态与落盘结果：在 with 块**之前**声明，这样块内块外都能引用，
        # 不必依赖"块内一定赋值过"（那种假设在加分支时极易失效）。
        state: GameSessionState | None = None
        saved_path: Path | None = None

        # ② 组装 1001
        login_packet = AccountClientInfoLoginPacket.for_platform_user(user_id)

        # 【为什么这里可以直接 new 一个 GameClient，而其他任务不行】
        # 登录**就是**会话状态的来源：此刻还没有令牌可注入，信封必须是干净的
        # （空 token / playerId=0，与 88 字节握手包一致）。
        # 其他业务任务一律走 ``BaseTask.open_game_client()``。
        with GameClient(logger=self.logger) as game:
            if self.dry_run:
                # 演练时没有真实握手，但**长度必须与真实发包一致** ——
                # 否则"演练看着对、真发时字节不同"，演练就白做了。
                # 这里用长度相同的占位指纹（32 个 0）。
                game.envelope.activity = SpecialActivityRefreshClass(
                    activeMission="0" * 32, puzzleRoleGroup=""
                )
                request = game.build(login_packet, include_defaults=True)
                print(request.describe())
                print()
                print("—— 以上为演练输出，未发送任何请求 ——")
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=f"演练完成：1001 请求体 {len(request.body)} 字节，未发送",
                    data={"user_id": user_id, "body_size": len(request.body)},
                )

            # ③ 先握手：取服务端下发的配置指纹。
            #    1001 需要把它回传（抓包实测如此），缺了会被服务端拒绝 ——
            #    实测不带它发 1001，回来的错误响应字段结构完全不同，
            #    会让人误以为是字段编号猜错了。
            server_config = game.handshake()
            self.logger.info("握手完成：%s", server_config.summary())
            # 注：1001 只回传 activeMission（不是握手拿到的整个指纹）——
            # 这条规则由 GameEnvelope.encode_with 统一处理，见其注释。

            # ④ 发 1001，拿账号态 + 区服列表
            view = game.send(
                login_packet,
                expect=AccountClientInfoLoginRes,
                include_defaults=True,
            )
            account = view.decode_sub_packet(AccountClientInfoLoginRes)
            if account.errorCode != 0:
                # TODO(protocol): 成功码取值尚未实测确认（目前沿用最常见的 0）。
                # 在确认之前，这里只用于"挡住明显失败"，不当成完整校验。
                raise ApiError(account.errorCode, "账号登录被拒绝（1002 errorCode 非 0）")
            self.logger.info(
                "账号登录成功：accountID=%d，区服 %d 个，cdnUrl=%r",
                account.accountID,
                len(account.allServerList),
                account.cdnUrl,
            )

            # ④ 选服 / 只拉列表
            #
            # 【为什么要分成两条路】
            # 网页/窗口的流程是两段式：先"登录 → 拿区服列表"（让用户挑），
            # 再"带区服编号 → 真正进区"。第一段只要列表，绝不能因为
            # "配置里那个区已经不存在"而失败，所以它**不参与选服**；
            # 第二段才按显式编号（或既有规则）选，选不到就明确报错。
            if self.plan_only:
                return self._plan_only_result(account, user_id)

            server = self._select_server(account)
            self.logger.info("选中区服 #%d %s", server.serverID, server.serverName)

            # ⑤ 发 1003：回填 serverID + 平台 userId，真正进区
            #
            # ⚠️ 两处细节都是实测结论（依据真实报文，整包 178 字节）：
            #   1. ``platform_user_id`` **必须传** —— 它会填进 ``hamiSubNoList``，
            #      缺了服务端就无法定位账号，一律回 ``ABNORMAL_PACKET``；
            #   2. activity 指纹**要带**（信封字段 16）。曾经误判成"不能带"，
            #      那个结论来自把 HAR 里 85 字节的**握手包**错当成了 1003。
            zone_packet = build_login_packet(account, server, platform_user_id=user_id)
            zone_view = game.send(zone_packet, expect=LoginRes, include_defaults=True)
            # ⚠️ 1004 的**信封元信息里没有令牌** —— 它藏在子包的
            # ``playerInitClass.token``（见 models/server.py 的 LoginRes）。
            # 所以要显式取出来回填进信封，否则后续每个业务请求都会因为没有
            # 令牌被拒，而错误信息只会说一句含糊的"包异常"。
            login_res = zone_view.decode_sub_packet(LoginRes)
            # 1004 的 errorCode 以前**完全没判**：一旦它非 0，我们会继续往下走，
            # 拿到一个空的 playerInitClass，最后只报"响应里没有 playerInitClass" ——
            # 那句话把"进区被拒"说成了"字段缺失"，排查方向直接被带偏。
            if login_res.errorCode != 0:
                raise ApiError(
                    login_res.errorCode,
                    "进区登录被拒绝（1004 errorCode 非 0）"
                    "；与 1001 不同，这里的失败多与账号/区服有关（例如角色不存在）",
                )
            if login_res.playerInitClass is None:
                raise ApiError(login_res.errorCode, "1004 响应里没有 playerInitClass")
            game.envelope.token = login_res.playerInitClass.token
            game.envelope.player_id = login_res.playerInitClass.playerID

            token = game.envelope.token
            player_id = game.envelope.player_id

            # ⑥ 把会话**装进唯一载体**，再分别交给"同进程"与"跨进程"
            #
            # 角色名解析：优先 1002 server.character_name（格式已反汇编证实，最准），
            # 其次 1004 myIconInfo.accNickName，再兜底 playerInitClass.name
            player_name = (
                server.character_name
                or (
                    login_res.myIconInfo.accNickName
                    if getattr(login_res, "myIconInfo", None)
                    else ""
                )
                or (
                    login_res.playerInitClass.name
                    if login_res.playerInitClass
                    else ""
                )
            )

            # ★ 2026-10-05：把 1004 里那份**角色名册快照**一并收进会话状态。
            #
            # 【为什么值得占这点空间】角色数据本来就在 1004（客户端登录后根本
            # 不发 5011、打开英雄界面也不发请求），但 1004 只在**登录这一刻**
            # 拿得到。而本工具的每条命令都是新进程、复用会话文件时**不会重登**
            # —— 不落盘的话，"角色查看"在复用会话这条路径上就永远是空的。
            #
            # 【为什么这里不判空】`roleInitClass` 是登录响应的一部分，服务端
            # 不给就是空列表；空列表会让 `roles` 任务走"快照也是空的"分支并
            # **如实报告**，不需要在这里提前抛错 —— 登录本身是成功的。
            role_snapshot = role_snapshot_from(login_res.roleInitClass)

            # ★ 2026-10-05：**头像编号快照**。协议里头像只是编号（无图片 URL），
            #   图片本体在热更资源里 —— 编号先落库（首次进区即有），图片能否解析
            #   出来见 services/avatar_service.py（解析不到就 None，前端回退首字）。
            #   另外把 1002 的 cdnUrl 一并打进日志：那是热更/资源下发的线索，
            #   tools/probe_avatar_cdn.py 靠它定位头像图集的下载位置。
            icon_info = getattr(login_res, "myIconInfo", None)
            player_init = login_res.playerInitClass
            avatar_ids = {
                "icon": int(getattr(player_init, "icon", 0) or 0),
                "frame": int(getattr(player_init, "frameId", 0) or 0),
                "representative": int(getattr(player_init, "roleRepresentativeId", 0) or 0),
                "role_main": int(getattr(icon_info, "roleMainID", 0) or 0),
                "skin": int(getattr(icon_info, "skinId", 0) or 0),
                "bg": int(getattr(icon_info, "bgId", 0) or 0),
            }
            avatar_data = resolve_avatar_png(avatar_ids)
            if avatar_data:
                self.logger.info("已解析头像图片（icon=%d）", avatar_ids["icon"])
            else:
                self.logger.info(
                    "头像暂无图片来源（icon=%d）：已记编号，前端先用昵称首字兜底",
                    avatar_ids["icon"],
                )

            state = GameSessionState.from_envelope(
                game.envelope,
                account_id=account.accountID,
                server_id=server.serverID,
                server_name=server.serverName,
                player_name=player_name,
                platform_user_id=user_id,
                roles=role_snapshot,
                avatar_data=avatar_data,
            )
            # ① 同进程：tasks/daily.py 把同一个 session 交给每个子任务，天然共享
            self.session.set_game_state(state)
            # ② 账号库：记录"这个号上次进的是哪个角色"并持久化该区专属 Token
            character_level = (
                getattr(getattr(login_res, "myIconInfo", None), "accLV", 0)
                or getattr(server, "character_level", 0)
                or 0
            )
            self._remember_game_progress(
                state, character_level=character_level, avatar_ids=avatar_ids
            )
            # ② 跨进程：写进会话文件（可用 CHERRYTALE_SAVE_SESSION=0 关闭）
            saved_path = self._persist_session(state)

        if not token or state is None:
            raise EnvelopeError(
                "1004 响应没有回填出会话令牌（RootPacket 的 token 字段为空）。\n"
                "  可能原因：还需要某个前置步骤，或 1003 的参数被拒绝。\n"
                "  排查建议：加 -v 查看完整响应字段（describe() 会列出所有元信息）"
            )

        self.logger.info("会话就绪：%s", state.describe())
        if state.roles:
            self.logger.info(
                "已记下角色名册快照：%d 个角色（来自 1004 roleInitClass，"
                "供「角色查看」与离线兜底用）",
                len(state.roles),
            )
        else:
            # 不报 error：登录是成功的。但如果服务端确实没下发角色，
            # 这条 WARNING 是唯一的线索 —— 否则用户只会看到"角色列表是空的"。
            self.logger.warning(
                "1004 响应里没有角色数据（roleInitClass 为空）："
                "「角色查看」将没有内容。若确认账号有角色，请加 -v 看完整响应字段"
            )
        if saved_path is None:
            self.logger.info(
                "会话**未**落盘：下一条命令需要自己带 "
                "%s（例如 `export %s=<令牌>`）",
                config.SESSION_TOKEN_ENV_NAME,
                config.SESSION_TOKEN_ENV_NAME,
            )
        else:
            self.logger.info(
                "后续命令可直接复用：python main.py run daily"
                "（令牌取自 %s；该文件含明文令牌，切勿提交或分享）",
                saved_path,
            )

        # ③ 内置钩子：登录签到（每日+活动）—— 每自然日首次进游戏自动发一次
        #    33055 并把三路入账写进开发者日志。失败只记 ✘ 不上抛，
        #    **绝不影响登录结果**（链路与幂等性见 tasks/login_signin.py）。
        run_if_first_login_today(state)

        role_label = (
            f"{state.player_name} (playerId={player_id})"
            if state.player_name
            else f"playerId={player_id}"
        )
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"登录成功：{role_label} @ {server.serverName}",
            data={
                "user_id": user_id,
                "account_id": account.accountID,
                "server_id": server.serverID,
                "server_name": server.serverName,
                "player_id": player_id,
                "player_name": player_name,
                "fighting_force": login_res.playerInitClass.fightingForce,
                "login_days": login_res.playerInitClass.loginDays,
                #: 1004 里带回来的角色名册条数（前端「角色查看」直接用它显示）
                "role_count": len(role_snapshot),
                "has_token": True,
                "session_saved": saved_path is not None,
                "session_file": str(saved_path) if saved_path is not None else "",
                # ⚠️ 这里**刻意不含令牌明文**：TaskResult 会被打印到终端、
                # 也可能被贴进日志。需要令牌的人请看会话文件（已打码的说明在日志里）。
            },
        )

    # ------------------------------------------------------------------
    # 选服：两种模式（只拉列表 / 按编号进区）
    # ------------------------------------------------------------------
    def _select_server(self, account: AccountClientInfoLoginRes) -> ServerListClass:
        """决定本次要进的区服。

        :param account: 1002 响应。
        :raises models.server.ServerSelectError: 显式指定的区服不在列表里，
            或按既有规则挑不出来时。

        :attr:`server_id` 有值时**只认它**（就是选区界面上点的那一个）：
        不在服务端下发的列表里就明确报错并列出可用编号 —— 绝不"悄悄换成别的区"，
        因为那会在玩家没注意的情况下把角色建到错误的地方。
        """
        if self.server_id is None:
            return choose_server_for_login(account)

        wanted = int(self.server_id)
        server = account.by_id(wanted)
        if server is None:
            ids = account.server_ids()
            preview = "、".join(str(item) for item in ids[:40])
            suffix = "…" if len(ids) > 40 else ""
            raise ServerSelectError(
                f"区服 #{wanted} 不在服务端下发的列表里（共 {len(account.allServerList)} 个区）。"
                f"可用编号：{preview}{suffix}"
            )
        self.logger.info("按显式指定进入区服：%s", server.label())
        return server

    def _plan_only_result(
        self, account: AccountClientInfoLoginRes, user_id: str
    ) -> TaskResult:
        """只拉区服列表（登录 / 选区两段式流程的**第一段**）。

        :param account: 1002 响应（``allServerList`` 就是要给用户看的列表）。
        :param user_id: 平台 userId（界面用来显示"当前是哪个平台号"）。
        :return: 成功结果；``data["servers"]`` 是 JSON 友好的列表。

        .. important::
           **本方法不写任何持久化状态**：不发 1003、不写会话文件、不写账号库、
           也不设置 ``self.session`` 的游戏状态。它只是"看一眼有哪些区"。

        【返回里为什么带 ``suggested_server_id``】
        界面上要默认高亮"按你现有配置会进的区"（配置 → 服务端建议 → 上次登录 →
        列表第一个）。取不到时留 ``None``，**不让整段请求失败** ——
        第一段的价值就是"总能看到列表"。
        """
        servers = [
            {
                "id": int(item.serverID),
                "name": item.serverName,
                "label": item.label(),
                "has_character": item.has_character(),
                "character_name": item.character_name,
                "character_level": item.character_level,
                "group_id": int(item.serverGroupID),
                "people_count": int(item.people_count),
            }
            for item in account.allServerList
        ]
        with_character = sum(1 for item in servers if item["has_character"])

        try:
            suggested: int | None = int(choose_server_for_login(account).serverID)
        except ServerSelectError as exc:
            self.logger.warning(
                "无法判断「按现有规则会进哪个区」（不影响候选列表）：%s", exc
            )
            suggested = None

        self.logger.info(
            "区服列表：共 %d 个（其中 %d 个已有角色）", len(servers), with_character
        )
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"已获取区服列表：共 {len(servers)} 个区，其中 {with_character} 个已有角色",
            data={
                "servers": servers,
                "account_id": account.accountID,
                "user_id": user_id,
                "suggested_server_id": suggested,
                # 只发过这两个包（1001 请求 / 1002 响应），没有任何写操作
                "planned_packets": (1001, 1002),
            },
        )

    # ------------------------------------------------------------------
    # 平台身份（"用哪个账号"）
    # ------------------------------------------------------------------
    def _resolve_platform_user_id(self) -> str:
        """确定本次进区用的平台账号，返回它的 ``userId``。

        :raises AuthenticationError: 所有来源都确定不了账号时
            （报错信息里带**可选账号列表**，不让使用者去猜）。

        【五级优先级 —— 越"显式"越优先】

        ====================  ==============================================
        1. ``--account`` 选择器   ``--account 2`` 或 ``CHERRYTALE_ACCOUNT=2``
                                  → 从账号库取该号，**快过期就自动续期**（免密码）
        2. 可用令牌               ``CHERRYTALE_AUTH_TOKEN`` / ``config_local.py``
                                  / ``.auth_token`` → 直接调 ``/api/v2/user`` 换 userId
        3. 账号密码               ``CHERRYTALE_LOGIN_ACCOUNT/PASSWORD`` 或
                                  ``.platform_credentials`` → 新登录 → **自动入库**
        4. 库里只有一个号         自动用它（第一次登录之后就不用再管了）
        5. 都没有 → 报错并列出可选账号
        ====================  ==============================================

        .. important::
           **为什么"选号"排在"令牌"前面**：``.auth_token`` 是单值文件，常常是
           很久以前留下的。如果它压过 ``--account``，就会出现"我明明选了 2 号，
           程序却拿着 1 号的令牌"—— 而且报错只会说令牌无效，极难定位。
           显式选号是"我想用哪个账号"的最直接表达，所以它优先。
           想临时指定令牌时**别加** ``--account`` 即可。
        """
        store = AccountStore()

        # ① 显式选号（+ 需要时自动续期）
        selector = self.account_selector
        if selector:
            saved = store.pick(selector)
            saved, refreshed = store.refresh_if_needed(saved)
            self._active_account = saved
            self.logger.info(
                "使用已保存的账号：%s%s",
                saved.account or saved.user_id,
                "（令牌已自动续期）" if refreshed else "",
            )
            return saved.user_id or saved.account

        # ② 手上已有可用令牌（环境变量 / config_local.py / .auth_token）
        if config.has_auth_token():
            with PlatformClient() as platform:
                platform_user = platform.get_user_info()
            self.logger.info(
                "平台身份确认（令牌来自环境变量/config_local/.auth_token）："
                "userId=%s（金币 %d）",
                platform_user.user_id,
                platform_user.coins,
            )
            return platform_user.user_id

        # ③ 有账号密码 → 换令牌并入库
        credentials = resolve_credentials()
        if credentials is not None:
            email, password = credentials
            self.logger.info("用账号密码换取平台令牌（来源：%s）", describe_source())
            with PlatformClient() as platform:
                token = platform.login_by_credentials(email, password)
            saved = SavedAccount(
                account=email,
                user_id=token.user_id,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                device_id=config.DEVICE_ID,
                obtained_at=token.obtained_at,
                expires_at=token.expires_at(),
                last_login_at=time.time(),
            )
            store.upsert(saved)
            self._active_account = saved
            self.logger.info(
                "已记入账号库：%s（账号库现有 %d 个）",
                saved.describe(),
                len(store.list_accounts()),
            )
            return saved.user_id or saved.account

        # ④ 库里只有一个号 → 自动用它
        accounts = store.list_accounts()
        if len(accounts) == 1:
            saved, refreshed = store.refresh_if_needed(accounts[0])
            self._active_account = saved
            self.logger.info(
                "使用账号库里唯一的账号：%s%s",
                saved.account or saved.user_id,
                "（令牌已自动续期）" if refreshed else "",
            )
            return saved.user_id or saved.account

        # ⑤ 都不行 → 明确报错，并把"下一步怎么做"写清楚
        raise AuthenticationError(
            "无法确定要用哪个平台账号。\n"
            "  任选一种方式：\n"
            "    1. 首次登录 / 换号（登录后会自动记入账号库）：\n"
            f"       export {config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME}=<邮箱>\n"
            f"       export {config.PLATFORM_LOGIN_PASSWORD_ENV_NAME}=<密码>\n"
            "    2. 用已保存的账号：--account <序号|邮箱>\n"
            "    3. 临时用某个令牌：CHERRYTALE_AUTH_TOKEN=<token>\n"
            "  当前账号库：\n"
            + "\n".join(f"    {line}" for line in store.describe_all())
        )

    def _remember_game_progress(
        self,
        state: GameSessionState,
        *,
        character_level: int = 0,
        avatar_ids: dict[str, int] | None = None,
    ) -> None:
        """把这次进区的角色信息与区服令牌写回账号库（同账号下不同区服独立保存）。

        :param state: 刚落地的会话状态。
        :param character_level: 角色等级（1004 或 1002 下发）。
        :param avatar_ids: 1004 头像编号快照（见 ``services/avatar_service.py``）；
            ``avatar_png`` 能解析到图片就一并入库，解析不到留空 —— 编号永远在，
            图片来源（热更 CDN / 离线图集）以后补上即自动出图。
        """
        store = AccountStore()
        target = self._active_account
        if target is None and state.platform_user_id:
            target = store.find_account(state.platform_user_id)
        if target is None:
            return

        session_entry = {
            "server_id": int(state.server_id),
            "server_name": state.server_name,
            "player_id": int(state.player_id),
            "player_name": state.player_name,
            "character_level": int(character_level),
            "account_id": int(state.account_id),
            "token": state.token,
            "time_stamp_token": state.time_stamp_token,
            "obtained_at": float(state.obtained_at or time.time()),
            "last_login_at": time.time(),
            "avatar": dict(avatar_ids) if avatar_ids else {},
            "avatar_png": state.avatar_data,
        }
        updated_sessions = dict(target.sessions)
        updated_sessions[str(state.server_id)] = session_entry

        updated = dataclass_replace(
            target,
            sessions=updated_sessions,
            game=session_entry,
            last_login_at=time.time(),
        )
        try:
            store.upsert(updated)
        except CherrytaleError as exc:
            # 账号库写不进去（例如已满）不该让"已经成功的登录"变成失败
            self.logger.warning("进区成功，但角色信息没能写回账号库：%s", exc)
            return
        self._active_account = updated
        self.logger.info("账号库已记录本次角色与区服凭据：%s @ #%d %s", updated.account or updated.user_id, state.server_id, state.player_name)

    # ------------------------------------------------------------------
    # 会话落盘
    # ------------------------------------------------------------------
    def _persist_session(self, state: GameSessionState) -> Path | None:
        """把会话状态写进会话文件，供**下一条命令**复用。

        :param state: 刚从 1004 构造出来的会话状态。
        :return: 写好的文件路径；未启用落盘或写入失败时返回 ``None``。

        【为什么失败不算任务失败】
        落盘只是"方便下一条命令"，本次会话（同进程内的后续任务）已经能用了。
        因为磁盘权限之类的原因把整个登录判定为失败，会让人误以为登录没成功。
        所以这里只记一条 warning。
        """
        if not config.SESSION_SAVE_ENABLED:
            return None
        # 【测试环境守卫】pytest 里**绝不允许**写真实会话文件。
        # 实测踩过：一次全量 pytest 把 `.session.json` 覆盖成了测试夹具的假令牌
        # （`FAKETOKEN…` + 另一个 playerId），之后真机任务就带着假令牌发包，
        # 现象是"读得到状态、打不了架"——极难定位。测试要落盘请自行把
        # ``CHERRYTALE_SESSION_FILE`` 指向临时文件（见 tests/test_login.py 的做法）。
        if os.environ.get("PYTEST_CURRENT_TEST") and session_file_path() == config.SESSION_FILE:
            self.logger.debug("检测到 pytest 环境：跳过写真实会话文件")
            return None
        try:
            path = save_session_state(state)
        except OSError as exc:  # 只读目录 / 权限不足 / 磁盘满
            self.logger.warning("会话文件写入失败（本次会话仍可用）：%s", exc)
            return None
        return path
