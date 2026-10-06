"""登录链路里的「区服」报文与选服逻辑（消息号 1001 / 1002 / 1003）。

【这一层解决什么问题】
Cherrytale 的登录是**两步**：

    1001 AccountClientInfoLoginPacket   → 账号登录（拿账号态）
    1002 AccountClientInfoLoginRes      → 响应里带 **区服列表**（allServerList 等）
    1003 LoginPacket（字段 2 = serverID）→ 把选中的 **区服编号** 回传，才真正进游戏

所以「选服」不是一个可选装饰，而是**必经的一步**：不选服就拿不到 1004 LoginRes。
关键在于——**区服编号只能从 1002 的响应里取**，客户端自己算不出来。
客户端 UI（dump.cs 的 ``Login_Main_serverlist_UIView``）做的也是同一件事：
把列表摆出来 → 玩家点一个 → 把这个 ``serverID`` 填进 LoginPacket。

本模块把这三件事做掉：**解析列表 / 挑一个区服 / 生成带 serverID 的登录包**。
真正的网络收发不在这里（见 models/envelope.py 与 client/），
这样「选服逻辑」可以完全离线用真实抓包字节验证。

【字段编号依据】
普通协议类「编号 = 声明顺序」（已由 ``VersionControlServerPacket`` 实测证实）。
本模块涉及的三个类的编号，**又都额外被 1002 的真实响应字节验证过**：

    AccountClientInfoLoginRes   #3 = allServerList（repeated ServerListClass）
                                #4 = suggestedServer（单个 ServerListClass）
    ServerListClass             #1 = serverID、#2 = serverName、#3 = serverState、
                                #4 = serverNote、#5 = people_count、
                                #6 = playerInfo、#7 = serverGroupID
    LoginPacket                 #1 = accountID、#2 = serverID

证据样本：``protocol_samples/gateway_samples.py`` 里 1002 那条响应
（8872 字节，夹具内联了前 256 字节，足够解出列表开头的几个区服）。

【⚠️ 一处刻意不做的事：不猜 ``serverState`` 的语义】
实测抓包（captures/cherrytale-fiddler-1.har 第 47 条）里 **167 个区服**：
166 个是 ``0``，只有 **1 个非零（= 3，恰好就是服务端建议的那个 #167 宮本武藏）**。
可见它确实会取非零值，但「0/1/3 = 正常 / 维护 / 爆满」这类对应关系在 dump.cs 里
没有可读的名字（只有 ``RPS_Const`` 里的数字）。
所以本模块**不**用它做任何过滤或判断 —— 猜错会把「能进的区服」判成不可用，
而现象是「程序说没有可用区服，游戏里却明明能进」。等确认了取值含义再补。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Sequence

from pydantic import Field

import config
from models.daily import ItemClass
from models.game_packet import ProtoMessage
from models.role import RoleClass


class ServerSelectError(ValueError):
    """选服失败（请求的区服不存在、列表为空、按组筛选后没有候选等）。

    刻意做成独立的异常类型：调用方（以及 ``BaseTask.run``）可以把它当成
    「业务失败」处理并继续跑后续流程，而不是让整条流水线崩掉。
    """


class ServerListClass(ProtoMessage):
    """一个区服条目（1002 响应里 ``allServerList`` 的元素类型）。

    .. note::
       字段名保持 dump.cs 原样（含 ``people_count`` 这种下划线写法）——
       编号是靠名字查 ``models/proto_fields.py`` 得到的，改名就要多维护一层映射。

    .. note::
       ``playerInfo`` 是服务端下发的角色概况字符串，格式为「角色名,等级」（如 "真昼,100"）。
       已由 GameAssembly.dll 反汇编（RPSUtility.Convert.ServerListClass_To_ClientForm）证实：
       客户端正是以逗号拆分，前半为 theServerCharacterName，后半为 theServerCharacterLV。
    """

    proto_class_name = "ServerListClass"

    #: 区服编号（**选服时唯一要用的字段**，登录包 1003 填的就是它）
    serverID: int = 0
    #: 区服名（实测形如 "仙度瑞拉" / "桃樂絲"，是角色名而非"服务器1"）
    serverName: str = ""
    #: 区服状态（实测 167 个区服里：166 个 0，仅 1 个为 3；
    #: 取值含义未确认，见模块文档，**不要**拿它做过滤）
    serverState: int = 0
    #: 区服备注（实测这批区服都是 "歡迎來到櫻境"）
    serverNote: str = ""
    #: 人数 / 人气值（实测 3.9 万 ~ 26 万，像是累积人气）
    people_count: int = 0
    #: 该账号在这个区的角色信息（**空字符串 = 这个区还没角**；
    #: 实测抓包里 167 个区服中有 **24 个**填了它，格式为 "角色名,等级"）
    playerInfo: str = ""
    #: 区服分组（实测 77 个区服是 -1＝未分组，其余按每 3 个一组排在组 1~30）
    serverGroupID: int = 0

    # ------------------------------------------------------------------
    # 便利判断
    # ------------------------------------------------------------------
    def has_character(self) -> bool:
        """本账号在这个区是否**已有角色**。

        判据是 ``playerInfo`` 非空 —— 服务端只会为「有角色」的区填这段信息。
        用途：优先挑「已经玩过的区」，可以避免无意中在新区开一个空号。
        """
        return bool(self.playerInfo.strip())

    @property
    def character_name(self) -> str:
        """本账号在该区服的角色名（未建角色返回空串）。"""
        raw = self.playerInfo.strip()
        if not raw:
            return ""
        return raw.split(",", 1)[0].strip()

    @property
    def character_level(self) -> int:
        """本账号在该区服的角色等级（未建角色或解析失败返回 0）。"""
        raw = self.playerInfo.strip()
        if not raw or "," not in raw:
            return 0
        part = raw.split(",", 1)[1].strip()
        try:
            return int(part)
        except ValueError:
            return 0

    def label(self) -> str:
        """一行可读标签，形如 ``#3 桃樂絲（组 1）``。

        日志里只打一个 ``serverID`` 数字，事后根本想不起来那是哪个区，
        所以凡是打印区服的地方都用这个方法。
        """
        return f"#{self.serverID} {self.serverName or '<未命名>'}（组 {self.serverGroupID}）"

    def summary(self) -> str:
        """更详细的一行摘要（带人数与角色情况），排查时用。"""
        if self.has_character():
            character = (
                f"已有角色({self.character_name} Lv.{self.character_level})"
                if self.character_name
                else "已有角色"
            )
        else:
            character = "无角色"
        return f"{self.label()} state={self.serverState} 人数={self.people_count} {character}"


@dataclass(frozen=True)
class ServerGroup:
    """选服界面的一个「分组」（= dump.cs 里 ``Login_Main_serverlist_UIView.ServerGroupData``）。

    :param group_id: 分组编号（= :attr:`ServerListClass.serverGroupID`）。
    :param servers: 该组下的区服，按 ``serverID`` 升序。

    【为什么要有这个概念】
    实测同一批区服的 ``serverGroupID`` 都是 1，看起来像"多余的字段"；
    但客户端 UI 是**先按组出页签、再列出组内区服**（``InitServerList`` 里那段
    ``GroupBy(x => x.serverGroupID)``）—— 一旦游戏开新分组（例如"新服"页签），
    按 `serverGroupID` 挑服就变得必要。先把这个结构留好，成本极低。
    """

    group_id: int
    servers: tuple[ServerListClass, ...]

    def ids(self) -> tuple[int, ...]:
        """本组所有区服的编号（升序）。"""
        return tuple(server.serverID for server in self.servers)


class AccountClientInfoLoginPacket(ProtoMessage):
    """账号登录请求（消息号 1001）—— 登录链路的**第一个业务包**。

    【取值已经有实测依据了】
    本类 15 个字段原先只能靠声明顺序推断（编号可信、**取值未知**）。
    后来从 Fiddler 的 HexView 复制到完整的 330 字节登录包，取值于是全部落定 ——
    锚点数据在 ``protocol_samples/session_samples.py``（``LOGIN_REQUEST_FIELDS``）。

    【其中一条推翻了原有认识，值得单独记住】
    原先以为登录要用 ``auerWebLogin_sid`` / ``auerWebLogin_token``（网页登录凭据）。
    实测完全不同：

    - ``auerWebLogin_sid`` = **0**（空置）；
    - 15 个字段里**根本没有** ``auerWebLogin_token``；
    - 真正的凭据是 **``socialAccount`` + ``socialOpenId`` = 平台 userId**
      （形如 ``ER00000000-0000-0000-0000-000000000000``，用平台 accessToken
      调 ``/api/v2/user`` 就能拿到）。

    也就是说：**登录不需要网页登录体系，也不需要密码**（``passWord`` 实测为空）。
    所以 ``config.ACCOUNT_TOKEN``（平台令牌）已经足够支撑游戏登录。

    .. warning::
       ``passWord`` / ``socialAccount`` / ``customAccount`` / ``auerWebLogin_sid``
       属于凭据类信息，已加入 ``masked_fields``：打印模型时自动打码，
       避免日志或截图泄露。
    """

    proto_class_name = "AccountClientInfoLoginPacket"

    masked_fields = frozenset(
        {"passWord", "auerWebLogin_sid", "socialAccount", "customAccount"}
    )

    #: 设备唯一标识。实测与 ``config.DEVICE_ID`` **一字不差**（两套体系共用同一个值）
    deviceID: str = ""
    #: 社交账号类型（实测 **9** = Erolabs 渠道，见 ``config.SOCIAL_ACCOUNT_TYPE_EL``）
    socialAccountType: int = 0
    #: 密码。**实测为空** —— 本游戏登录不走密码（游客 / 社交账号体系）
    passWord: str = ""
    #: 自定义账号名（实测 ``"ecchigame"``，是渠道标识而非玩家账号）
    customAccount: str = ""
    #: 社交账号 = **平台 userId**（真正的登录凭据）
    socialAccount: str = ""
    #: 设备型号（实测 ``"Dell G15 5520 (Dell Inc.)"``）
    deviceModel: str = ""
    #: 系统版本（实测 ``"Windows 11  (10.0.26200) 64bit"``）
    osVersion: str = ""
    #: 客户端版本（实测 ``2.2.0-el-h-win``，见 ``config.GAME_CLIENT_VERSION``）
    clientVersion: str = ""
    #: 渠道 / 平台类型（实测 **2**，与握手包一致）
    channelPlatformType: int = 0
    #: 广告标识（实测为空）
    advertisementID: str = ""
    #: 账号创建类型（实测 **0**；服务端据此判断"新号"还是"登录"）
    createAccountType: int = 0
    #: 社交平台 OpenID（实测与 ``socialAccount`` 同值）
    socialOpenId: str = ""
    #: 语言代码（实测 ``zhcn``）
    languageCode: str = ""
    #: 网页登录会话 ID。**实测为 0** —— 它不是本游戏的登录凭据
    auerWebLogin_sid: int = 0
    #: 设备是否已 root / 越狱（实测 0）
    isRoot: bool = False

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def for_platform_user(
        cls,
        user_id: str,
        *,
        device_id: str | None = None,
        device_model: str | None = None,
        os_version: str | None = None,
        client_version: str | None = None,
        channel_platform_type: int | None = None,
        language_code: str | None = None,
    ) -> "AccountClientInfoLoginPacket":
        """按**平台 userId** 构造登录包（= 实测到的登录方式）。

        :param user_id: 平台 userId，形如 ``ER00000000-…``。
            用平台 accessToken 调 ``/api/v2/user`` 即可拿到。
        :param device_id: 设备标识；``None`` → ``config.DEVICE_ID``（实测同值）。
        :param device_model: 设备型号；``None`` → ``config.DEVICE_MODEL``。
        :param os_version: 系统版本；``None`` → ``config.OS_VERSION``。
        :param client_version: 客户端版本；``None`` → ``config.GAME_CLIENT_VERSION``。
        :param channel_platform_type: 渠道类型；``None`` → ``config.CHANNEL_PLATFORM_TYPE_EL``。
        :param language_code: 语言；``None`` → ``config.GAME_LANGUAGE``。
        :raises ValueError: ``user_id`` 为空时。

        .. note::
           把「哪些字段来自配置、哪些来自账号」在这一个地方写清，
           调用方（``tasks/login.py``）就不必再关心字段细节；
           将来若实测发现某个字段变了，也只需改这里。
        """
        if not user_id.strip():
            raise ValueError(
                "user_id 为空，无法构造登录包。\n"
                "  它应来自平台侧：GET /api/v2/user 的 data.userId"
            )
        return cls(
            deviceID=device_id if device_id is not None else config.DEVICE_ID,
            socialAccountType=config.SOCIAL_ACCOUNT_TYPE_EL,
            passWord="",  # 实测为空：本游戏登录不走密码
            customAccount=config.GAME_CUSTOM_ACCOUNT,
            socialAccount=user_id,
            deviceModel=device_model if device_model is not None else config.DEVICE_MODEL,
            osVersion=os_version if os_version is not None else config.OS_VERSION,
            clientVersion=(
                client_version
                if client_version is not None
                else config.GAME_CLIENT_VERSION
            ),
            channelPlatformType=(
                channel_platform_type
                if channel_platform_type is not None
                else config.CHANNEL_PLATFORM_TYPE_EL
            ),
            advertisementID="",  # 实测为空
            createAccountType=0,  # 实测为 0
            socialOpenId=user_id,  # 实测与 socialAccount 同值
            languageCode=(
                language_code if language_code is not None else config.GAME_LANGUAGE
            ),
            auerWebLogin_sid=0,  # 实测为 0：不使用网页登录体系
            isRoot=False,  # 实测 0
        )


class AccountClientInfoLoginRes(ProtoMessage):
    """账号登录响应（消息号 1002）—— **区服列表的唯一来源**。

    实测这条响应有 8872 字节（抓包里就是它），其中大部分内容就是区服列表：
    每个区服的名称、备注、人数，以及「本账号在该区有没有角色」。

    .. note::
       成功时 ``errorCode`` 实测为 0；非 0 时列表通常为空。所以
       :meth:`choose_server` 在挑不到区服时会明确报错，而不是返回一个
       ``serverID = 0`` 的假对象 —— 后者会让下一个请求带着 0 去登录，
       而服务端只会回一句含糊的错误。
    """

    proto_class_name = "AccountClientInfoLoginRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 账号 ID（**登录包 1003 的字段 1 就是它**）
    accountID: int = 0
    #: 全部区服
    allServerList: list[ServerListClass] = Field(default_factory=list)
    #: 服务端建议的区服（通常是最近登录过的那个）
    suggestedServer: ServerListClass | None = None
    #: 最近登录过的区服（最近的在前）
    lastLoginedServerList: list[ServerListClass] = Field(default_factory=list)
    #: 后端开发模式标记（正常为 0）
    backendDevelopMode: int = 0
    #: 论坛 / BBS 标识
    bbsid: str = ""
    #: 该账号可用的 CDN 地址
    cdnUrl: str = ""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def by_id(self, server_id: int) -> ServerListClass | None:
        """按编号取区服；不存在返回 ``None``（不抛异常，方便调用方自行处理）。"""
        for server in self.allServerList:
            if server.serverID == server_id:
                return server
        return None

    def server_ids(self) -> tuple[int, ...]:
        """全部区服编号（保持响应里的顺序）。"""
        return tuple(server.serverID for server in self.allServerList)

    def describe_servers(self) -> str:
        """把区服列表渲染成多行文本（日志 / 工具输出用）。

        形如::

            区服列表（共 3 个）：
              #1 仙度瑞拉（组 1）state=0 人数=261612 无角色  ← 服务端建议
              #2 希爾（组 1）state=0 人数=... 无角色
        """
        if not self.allServerList:
            return "区服列表为空（服务端没有下发任何区服）"
        suggested_id = self.suggestedServer.serverID if self.suggestedServer else None
        lines = [f"区服列表（共 {len(self.allServerList)} 个）："]
        for server in self.allServerList:
            mark = "  ← 服务端建议" if server.serverID == suggested_id else ""
            lines.append(f"  {server.summary()}{mark}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 选服
    # ------------------------------------------------------------------
    def choose_server(
        self,
        *,
        server_id: int | None = None,
        group_id: int | None = None,
        prefer_suggested: bool = True,
        prefer_with_character: bool = False,
    ) -> ServerListClass:
        """挑一个区服，返回**完整的** :class:`ServerListClass`（不只是编号）。

        :param server_id: 显式指定的区服编号（优先级最高）。来自
            ``config.SERVER_ID``（环境变量 ``CHERRYTALE_SERVER_ID``）。
        :param group_id: 只在指定分组里挑（``None`` = 不限制分组）。
        :param prefer_suggested: 没有显式指定时，是否优先用服务端建议的区服。
        :param prefer_with_character: 没有显式指定时，是否优先挑「本账号已有角色」的区服。
        :raises ServerSelectError: 显式指定的区服不在列表里，或（过滤后）没有任何候选。

        挑选优先级（**顺序是有意为之的**）：

        1. 显式配置的 ``server_id`` —— 你说了算；不在列表里就直接报错，
           **绝不"帮你换一个"**，否则你会以为程序按你的意思选对了；
        2. 服务端建议的区服（``suggestedServer``）—— 等于玩家上次玩的那个；
        3. ``lastLoginedServerList`` 里的第一个 —— 退一步的历史记录；
        4. ``allServerList`` 里编号最小的那个 —— 兜底，保证新号也能进游戏。

        .. note::
           ``prefer_with_character`` 默认关闭：它会让「选哪个区」依赖账号历史，
           同一份代码跑两次可能选中不同的区。需要「只在玩过的区执行」时再显式打开。
        """
        # ---- 1. 显式指定：命中就用，没命中就报错（不做任何"智能替换"）----
        if server_id is not None:
            found = self.by_id(server_id)
            if found is None:
                available = "、".join(str(value) for value in self.server_ids()) or "<列表为空>"
                raise ServerSelectError(
                    f"指定的区服 serverID={server_id} 不在服务端下发的列表里。\n"
                    f"可用区服：{available}\n"
                    "请用环境变量 CHERRYTALE_SERVER_ID 改成其中之一，"
                    "或清空该配置让程序自动挑。"
                )
            return found

        # ---- 2~4. 自动挑：候选先过分组 / 「有角色」两道过滤 ----
        candidates = self._filtered_candidates(
            group_id=group_id, with_character=prefer_with_character
        )

        if prefer_suggested and self.suggestedServer is not None and self.suggestedServer.serverID:
            suggested = self._in_candidates(candidates, self.suggestedServer.serverID)
            if suggested is not None:
                return suggested

        for server in self.lastLoginedServerList:
            matched = self._in_candidates(candidates, server.serverID)
            if matched is not None:
                return matched

        if candidates:
            return min(candidates, key=lambda item: item.serverID)

        raise ServerSelectError(self._empty_candidates_reason(group_id, prefer_with_character))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _filtered_candidates(
        self, *, group_id: int | None, with_character: bool
    ) -> list[ServerListClass]:
        """按分组 / 「已有角色」过滤候选（保持响应里的顺序）。"""
        result: list[ServerListClass] = []
        for server in self.allServerList:
            if group_id is not None and server.serverGroupID != group_id:
                continue
            if with_character and not server.has_character():
                continue
            result.append(server)
        return result

    @staticmethod
    def _in_candidates(
        candidates: Sequence[ServerListClass], server_id: int
    ) -> ServerListClass | None:
        """在**已过滤的**候选里找指定编号（找不到返回 ``None``）。

        这一步保证「建议区」不会绕过 ``group_id`` / ``with_character`` 过滤器：
        否则会出现「我明明限定第 3 组，程序却选了第 1 组的区」这种自相矛盾的行为。
        """
        for server in candidates:
            if server.serverID == server_id:
                return server
        return None

    def _empty_candidates_reason(self, group_id: int | None, with_character: bool) -> str:
        """没有候选时给出**具体**原因（而不是一句笼统的「选服失败」）。"""
        problems: list[str] = []
        if not self.allServerList:
            problems.append(
                "服务端下发的 allServerList 为空"
                f"（errorCode={self.errorCode}）—— 账号登录可能没成功"
            )
        if group_id is not None:
            groups = sorted({server.serverGroupID for server in self.allServerList})
            problems.append(f"分组号 {group_id} 里没有区服（现有分组：{groups or '<无>'}）")
        if with_character:
            problems.append("没有任何区服满足「本账号已有角色」")
        if not problems:  # pragma: no cover - 目前所有分支都会产生原因
            problems.append("没有可用的区服")
        return "选服失败：" + "；".join(problems)


def group_servers(servers: Iterable[ServerListClass]) -> tuple[ServerGroup, ...]:
    """把区服按 ``serverGroupID`` 分组（对应客户端选服界面的页签）。

    :return: 按组号升序排列的分组；组内按 ``serverID`` 升序。

    客户端 ``Login_Main_serverlist_UIView.InitServerList`` 就是这么组织的
    （``GroupBy(x => x.serverGroupID)``）：先出页签，再列组内区服。
    我们做自动化时通常不需要它，但排查「为什么某区服不在列表里」时，
    按组打印一遍能一眼看出是"整组都没下发"还是"只少了某一个"。
    """
    buckets: dict[int, list[ServerListClass]] = {}
    for server in servers:
        buckets.setdefault(server.serverGroupID, []).append(server)
    return tuple(
        ServerGroup(
            group_id=group_id,
            servers=tuple(sorted(items, key=lambda item: item.serverID)),
        )
        for group_id, items in sorted(buckets.items())
    )


class LoginPacket(ProtoMessage):
    """真正的登录包（消息号 1003）—— **区服编号在这里生效**。

    与 1001（账号登录）的分工：

    - 1001 回答「**你是谁**」（设备、渠道、账号），响应给出区服列表；
    - 1003 回答「**你要进哪个区**」，字段 2 ``serverID`` 就是上一步选出来的编号。

    .. note::
       本模型的**取值已逐字节核对**（依据：真实的 1003 请求原始报文，整包 178 字节）。
       三条最容易踩的坑写在字段注释里：``auerWebLogin_token`` 是字符串 ``"-1"``
       （不是空串）、``hamiSubNoList`` **必须**带平台 userId、以及这个包要
       **带上**信封的活动指纹（字段 16）。
    """

    proto_class_name = "LoginPacket"

    masked_fields = frozenset({"auerWebLogin_token", "auerWebLogin_sid"})

    #: 账号 ID（来自 1002 响应的 ``accountID``）
    accountID: int = 0
    #: **区服编号**（选服结果，来自 1002 响应的 ``allServerList``）
    serverID: int = 0
    #: 网页登录会话 ID（实测为 0）
    auerWebLogin_sid: int = 0
    #: 网页登录令牌。**实测值是字符串 ``"-1"``，不是空串** —— 与 ``settingMd5``
    #: 同一套「占位 = -1」约定，表示本客户端不走网页登录体系。
    auerWebLogin_token: str = "-1"
    #: 渠道 / 平台类型（与 1001 保持一致）
    channelPlatformType: int = 0
    #: 客户端版本
    clientVersion: str = ""
    #: 平台账号标识列表。**这里必须填平台 userId**：实测值是
    #: ``["ER00000000-0000-0000-0000-000000000000"]``，与 1002 响应的 ``bbsid``
    #: 完全相同。字段名里的 "Hami" 是台湾渠道的历史遗留，但它真正承载的是
    #: **账号凭据** —— 留空时服务端无法定位账号，直接回 ``ABNORMAL_PACKET``。
    hamiSubNoList: list[str] = Field(default_factory=list)
    #: 语言代码
    languageCode: str = ""

    @classmethod
    def for_server(
        cls,
        server: ServerListClass,
        *,
        account_id: int = 0,
        platform_user_id: str = "",
        channel_platform_type: int = 0,
        client_version: str = "",
        language_code: str = "",
        auer_web_login_sid: int = 0,
        auer_web_login_token: str = "-1",
    ) -> LoginPacket:
        """按选定的区服生成登录包（``serverID`` 自动填入）。

        :param server: :meth:`AccountClientInfoLoginRes.choose_server` 的返回值。
        :param account_id: 1002 响应里的 ``accountID``。
        :param platform_user_id: **平台侧 userId**（形如 ``ER00000000-…``）。
            会填进 ``hamiSubNoList`` —— 实测这是进区能否成功的关键：
            留空则服务端一律回 ``ABNORMAL_PACKET``。
        :raises ServerSelectError: 传入的区服 ``serverID`` 为 0（协议里表示「未指定」）时。

        .. warning::
           这里对 ``serverID == 0`` 显式报错。带着 0 去登录，服务端通常只回一个
           笼统的错误码；与其到那时再排查，不如在组包阶段就拦下来。
        """
        if not server.serverID:
            raise ServerSelectError(
                "区服对象的 serverID 为 0（协议里表示「未指定」），不能用来登录。\n"
                "请先用 AccountClientInfoLoginRes.choose_server() 从 1002 响应里挑一个区服。"
            )
        return cls(
            accountID=account_id,
            serverID=server.serverID,
            auerWebLogin_sid=auer_web_login_sid,
            auerWebLogin_token=auer_web_login_token,
            channelPlatformType=channel_platform_type,
            clientVersion=client_version,
            hamiSubNoList=[platform_user_id] if platform_user_id else [],
            languageCode=language_code,
        )


# ---------------------------------------------------------------------------
# 进区登录（1003 → 1004）：**会话令牌在这里**
# ---------------------------------------------------------------------------
class PlayerClass(ProtoMessage):
    """玩家角色信息（``LoginRes.playerInitClass`` 的类型，dump.cs 435602 行）。

    **会话令牌就在字段 1 ``token``**（实测 34 字符，如 ``uef9bucrnl3219lr…``）。
    它必须回填进信封（字段 2），后续**所有**业务请求都要带上。

    ⚠️ 它**不在信封元信息里** —— 1004 的信封只有 ``packetID`` /
    ``timeStampToken`` / ``PlayerBasicDataClass`` / ``activity`` 四项，
    所以只能专门到子包里取。这一点曾经让登录"看着成功却拿不到令牌"。
    """

    proto_class_name = "PlayerClass"

    #: 会话令牌属于敏感信息，打印时要遮掉
    masked_fields = frozenset({"token"})

    #: 会话令牌（后续请求放进信封字段 2）
    token: str = ""
    #: 玩家 ID（信封字段 8）
    playerID: int = 0
    #: 角色名
    name: str = ""
    #: 是否已取过名
    named: bool = False
    #: 角色经验
    playerExp: int = 0
    #: 头像编号
    icon: int = 0
    #: VIP 经验
    vipExp: int = 0
    #: 战力
    fightingForce: int = 0
    #: 上次登录时间
    lastLoginTime: int = 0
    #: 代表角色 sid
    roleRepresentativeSid: int = 0
    #: 登录天数
    loginDays: int = 0
    #: 今日是否首次登录
    isTodayFristLogin: int = 0
    #: 段位经验
    rankExp: int = 0
    #: 头像框
    frameId: int = 0
    #: 代表角色 ID
    roleRepresentativeId: int = 0
    #: 建号时间
    createTime: int = 0


class IconClass(ProtoMessage):
    """玩家头像与昵称信息（``LoginRes.myIconInfo`` 的类型，dump.cs 434671 行）。"""

    proto_class_name = "IconClass"

    accType: int = 0
    accName: str = ""
    accLV: int = 0
    accVip: int = 0
    roleMainID: int = 0
    accNickName: str = ""
    socialOpenId: str = ""
    isShowMySocialIcon: bool = False
    frameId: int = 0
    serverId: str = ""
    bgId: int = 0
    effectId: int = 0
    serverGroupID: int = 0
    titleID: int = 0
    isBlocked: int = 0
    bgmId: int = 0
    playerID: int = 0
    skinId: int = 0
    allianceTitle: int = 0


class EquipClass(ProtoMessage):
    """一件装备（``LoginRes.equipInitClass`` 的元素类型，dump.cs 433082 行）。

    :ivar equipSid: 装备**实例** ID。
    :ivar equipID: 装备**模板** ID（查装备配置表用）。
    :ivar equipLV: 装备等级。
    :ivar roleSid: 穿戴者角色实例 sid；``0`` 表示没人穿。
    :ivar lockStates: 锁定状态。
    :ivar quenchingInfo: 淬炼信息（未建模，保留原始字节）。

    .. note::
       装备与角色、道具一样，**登录 1004 就一次性全量下发** ——
       客户端登录后不会再去单独拉装备清单。
    """

    proto_class_name = "EquipClass"

    #: 装备实例 ID
    equipSid: int = 0
    #: 装备模板 ID
    equipID: int = 0
    #: 装备等级
    equipLV: int = 0
    #: 穿戴者角色实例 sid（0 = 未穿戴）
    roleSid: int = 0
    #: 锁定状态
    lockStates: int = 0
    #: 淬炼信息（未建模，保留原始字节）
    quenchingInfo: list[bytes] = Field(default_factory=list)


class TeamClass(ProtoMessage):
    """一套队伍编成（``LoginRes.teamInitClass`` 的元素类型，dump.cs 437215 行）。

    :ivar teamID: 队伍编号。
    :ivar rolesSid: 该队伍的成员角色**实例** sid 列表。
    :ivar teamUse: 用途（哪一类战斗用这套编成）。
    :ivar mirrorBuffID: 镜像卡增益。
    :ivar flagID: 队伍标记。

    .. note::
       ``rolesSid`` 里装的是 :class:`models.role.RoleClass` 的 ``roleSid`` ——
       与挖矿 ``roleSidList``、装备 ``roleSid`` 是**同一套实例 ID**。
    """

    proto_class_name = "TeamClass"

    #: 队伍编号
    teamID: int = 0
    #: 成员角色实例 sid
    rolesSid: list[int] = Field(default_factory=list)
    #: 用途
    teamUse: int = 0
    #: 镜像卡增益
    mirrorBuffID: int = 0
    #: 队伍标记
    flagID: int = 0


class LoginRes(ProtoMessage):
    """进区登录的响应（消息号 1004）—— **会话令牌与全量初始化数据都在这里**。

    .. important::
       **这是整个工具最重要的一份响应。** 除了令牌，服务端还在同一个包里
       一次性下发玩家全部"家当"的快照：

       ====================  =========  ===================================
       字段                  类型        内容
       ====================  =========  ===================================
       2 ``playerInitClass`` 单条       玩家基本信息（**令牌在里面**）
       3 ``myIconInfo``      单条       头像 / 昵称
       4 ``equipInitClass``  列表       **装备**
       5 ``roleInitClass``   列表       **角色名册（英雄）** ← 前端「角色查看」的数据源
       6 ``itemInitClass``   列表       **背包道具**
       7 ``teamInitClass``   列表       **队伍编成**
       ====================  =========  ===================================

       **客户端登录后不会再去单独拉这几样**：实测登录抓包里没有 5011
       （角色名册），打开英雄界面也**一条网关请求都没有**
       （``captures/英雄查看.saz`` 里 0 条 ``game-ct-labs.ecchi.xxx:1893`` 会话）。
       ⇒ 英雄数据只能来自这里。

    .. warning::
       ``LoginRes`` 共有 **28 个字段**，本模型**只声明了上面这 7 个**。
       这是**故意的**（未用到的初始化数据不建模，省得凭空猜语义），
       但要知道后果：**没声明的字段会被解码器当"未知字段"静默跳过**
       （``ProtoMessage.decode`` 对未声明编号只记一条 debug 日志）。
       而且 ``LoginRes`` **只解码、不编码**，所以 ``encode()`` 里那条
       "字段必须齐全"的校验**碰不到它** —— 漏声明字段**不会报错**，
       只会安静地拿不到数据。以后要取新数据时，先数一数这里声明了几个字段。

       （字段表见 ``models/proto_fields.py`` 的 ``LoginRes``。）
    """

    proto_class_name = "LoginRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: 玩家角色信息（**令牌在它的 ``token`` 字段**）
    playerInitClass: PlayerClass | None = None
    #: 玩家头像与昵称信息（包含 ``accNickName``）
    myIconInfo: IconClass | None = None
    #: 全部装备（登录快照）
    equipInitClass: list[EquipClass] = Field(default_factory=list)
    #: 全部角色（**英雄名册**，登录快照）—— 前端「角色查看」与挖矿挑角色的数据源
    roleInitClass: list[RoleClass] = Field(default_factory=list)
    #: 全部背包道具（登录快照）
    itemInitClass: list[ItemClass] = Field(default_factory=list)
    #: 全部队伍编成（登录快照）
    teamInitClass: list[TeamClass] = Field(default_factory=list)


#: 实测（``protocol_samples`` 里那条 1002 响应，夹具内联的前 256 字节）
#: 已经能看到的区服编号：1 / 2 / 3 / 4。
#:
#: 它**不是**"允许取值的白名单"（区服由服务端决定，随时会加），
#: 而是排查时的参照物：若某次响应里出现完全不同的编号，
#: 说明服务端换了区服划分，值得复核一遍字段对齐。
KNOWN_SAMPLE_SERVER_IDS: Final[tuple[int, ...]] = (1, 2, 3, 4)
