"""游戏会话状态：1004 令牌 + playerId + 区服，以及它在各层之间的传递方式。

【为什么需要一个独立的类，而不是把 token 塞在别处】

登录（1004 ``LoginRes``）产出的信息有**一组**，不止一个令牌：::

    令牌      playerInitClass.token    → RootPacket 字段 2，后续每个请求都要带
    playerId  playerInitClass.playerID → RootPacket 字段 8
    区服      serverID / serverName    → 后续请求不用，但报错时要能说清"在哪个区"
    账号      accountID                → 再进区（1003）时才需要

如果把这些字段散着放（一个放 ``GameSession``、一个放 ``GameEnvelope``、一个靠环境变量），
就会出现两个经典问题：

1. **漏传**：新写的任务只拿了 token、忘了 playerId —— 而服务端的反应只是含糊的"包异常"；
2. **混用**：把**游戏令牌**当成**平台令牌**塞进 HTTP 头（两者用途完全不同，
   见 ``crypto/sign.py`` 的 ``TokenStore`` 与 ``config.SESSION_TOKEN`` 的说明）。

所以这里做**唯一权威载体**：能不能发包，取决于手里有没有一个有效的 ``GameSessionState``。

【生命周期（谁负责哪一段）】

============================  ====================================================
阶段                           负责方
============================  ====================================================
产生                           ``tasks/login.py``（1004 解析后构造）
携带                           ``GameSession.game_state``（同进程）；会话文件 / 环境变量（跨进程）
注入协议                       :meth:`GameSessionState.apply_to` —— 写进信封的
                               ``token``（字段 2）与 ``player_id``（字段 8）
失效判断                       :meth:`is_valid`。服务端是否下发有效期**未实测**
                               （``PlayerClass`` 的 16 个字段里没有过期时间）
============================  ====================================================

.. warning::
   本类**不打印令牌明文**：``describe()`` 只显示前 4 个字符与长度。
   凭据一旦进了日志或截图，比"临时找不到令牌"麻烦得多。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Iterable, Mapping

from models.envelope import GameEnvelope

if TYPE_CHECKING:  # 仅用于类型注解 —— 避免与 models.role 形成运行期依赖
    from models.role import RoleClass

#: 会话文件里的结构版本号。将来字段有增删时靠它判断旧文件要不要重登，
#: 而不是靠猜（猜错的后果是拿半截状态去发包）。
#:
#: 版本 2：新增 ``time_stamp_token``（战斗类请求必需，见 ``GameSessionState`` 的说明）。
#: 版本 3：新增 ``roles``（登录 1004 的角色名册快照，供 ``roles`` 任务兜底）。
#: 版本 4：新增 ``avatar_data``（角色头像 base64，任务界面信息卡用）。
#:   —— 版本号只用于日志提示，``from_dict`` 一律**宽容解析**，
#:   所以旧文件不会因为版本落后而失效（缺的字段取默认值）。
SESSION_JSON_VERSION: Final[int] = 4

#: ``describe()`` 里令牌保留多少可见字符（够区分"是不是同一个令牌"即可）
_VISIBLE_CHARS: Final[int] = 4


def _mask_secret(value: str) -> str:
    """把凭据打码成 ``uef9…(34 字符)``。

    :param value: 原始凭据（令牌等）。
    :return: 打码后的字符串；空值返回 ``<空>``。

    保留前 4 个字符是有意的：排查时经常要回答"这条日志和上一条是不是同一个令牌"，
    4 个字符足以区分，又不构成可用的凭据。
    """
    if not value:
        return "<空>"
    return f"{value[:_VISIBLE_CHARS]}…({len(value)} 字符)"


@dataclass
class GameSessionState:
    """一次登录（1004）换来的会话状态。

    :param token: 游戏会话令牌（``PlayerClass.token``，实测 34 字符）。
    :param player_id: 玩家 ID（``PlayerClass.playerID``）。
    :param account_id: 账号 ID（``AccountClientInfoLoginRes.accountID``，再进区时用）。
    :param server_id: 本次进入的区服编号。
    :param server_name: 区服名（只用于人读，例如 ``宮本武藏(#167)``）。
    :param platform_user_id: 平台侧 userId —— 1003 的 ``hamiSubNoList`` 必须是它。
    :param obtained_at: 取得时刻（Unix 秒），用于排查"这份状态是不是今天登的"。
    :param expires_at: 到期时刻；``None`` 表示**服务端没有下发有效期**，
        因此不主动判过期（实测 ``PlayerClass`` 里确实没有过期字段）。

    .. note::
       ``token`` 与 ``player_id`` 会经 :meth:`apply_to` 写进 ``GameEnvelope``；
       其余字段**不进协议**，只用于日志与排查。
    """

    token: str = ""
    player_id: int = 0
    #: 角色名（如 "真昼"）。登录 1004 或 1002 选区下发；界面与日志展示用。
    player_name: str = ""
    #: 令牌时间戳（``RootPacket`` 字段 10）：服务端在**进区登录 1004** 的元信息里下发，
    #: 之后每个业务请求都要回传。默认 ``-1``（= 尚未使用）。
    #:
    #: ⚠️ 为什么必须持久化：实测**战斗类请求**不带它时会被前置层拦成
    #: ``{"response_code":"OK"}``（``content-type: text/html``，非游戏协议），
    #: 而命令行每次都是新进程 —— 不落盘就拿不到（详见 ``models/envelope.py`` 的
    #: ``update_from`` 与 ``notes/recon_findings.md`` 的 39007 排查记录）。
    time_stamp_token: int = -1
    account_id: int = 0
    server_id: int = 0
    server_name: str = ""
    platform_user_id: str = ""
    obtained_at: float = 0.0
    expires_at: float | None = None
    #: **登录 1004 的角色名册快照**（``LoginRes.roleInitClass`` 的瘦身版）。
    #:
    #: 【为什么把它放进会话状态】
    #: 角色数据本来就在 1004 里（客户端登录后根本不发 5011），但 1004 只在
    #: **登录那一刻**拿得到。而本工具的命令行/网页每次都是新进程，跨进程复用
    #: 会话文件时**不会重新登录** —— 若不把快照落盘，``roles`` 任务在"复用
    #: 会话"这条路径上就永远拿不到角色数据。
    #:
    #: 【为什么只存 wire 事实】
    #: 每项只存协议里真实存在的字段（``roleSid`` / ``roleID`` / ``roleLV`` /
    #: ``roleStar``），**不存**角色名、不存换算后的 RoleInfoID ——
    #: 那些是配置表推出来的，随时可能因游戏更新而变。缓存"推导结果"会让
    #: 数据在更新后静默失真，而缓存"原始事实"最多是过时，不会错。
    #:
    #: .. note::
    #:    ``roleID`` 是 **RoleMainID**（130xxxxxx），与矿点要的 RoleInfoID
    #:    不是一回事 —— 见 ``models.game_config.RoleIdMap``。
    roles: tuple[dict[str, int], ...] = ()
    #: **角色头像**（PNG 的 base64 串，无图时 ``None``）。
    #:
    #: 【为什么只存 base64 而不是文件路径】头像来源见 ``services/avatar_service.py``
    #: （热更 CDN / 离线图集，2026-10-05 探测定案）。它必须穿过两层边界到达前端：
    #: 会话文件（跨进程）→ ``/api/player``。存路径的话，Android 端 filesDir 与
    #: 桌面端工程根的路径不一致，还要为 ``<img>`` 单开一条免鉴权静态路由；
    #: 直接存图片字节（小图 5~30KB）最省事，也天然随会话文件走。
    avatar_data: str | None = None

    @property
    def has_token(self) -> bool:
        """是否持有令牌（不看过期）。"""
        return bool(self.token)

    def is_expired(self, *, now: float | None = None) -> bool:
        """令牌是否已过期。

        :param now: 用于测试的"当前时刻"（Unix 秒）；``None`` 表示取真实时间。
        :return: 未设置 ``expires_at`` 时**恒为 False** —— 服务端没下发有效期，
            我们就不该自己编一个（编短了会白白重登，编长了等于没判）。

        真正判断"令牌还有没有用"的可靠信号是**服务端响应**
        （见 ``models/game_error.py`` 的错误分类与 ``notes/open_questions.md`` 的 Q-010）。
        """
        if self.expires_at is None:
            return False
        current = time.time() if now is None else now
        return current >= self.expires_at

    def is_valid(self, *, now: float | None = None) -> bool:
        """当前的会话状态能否直接用于发包（有令牌且未过期）。"""
        return self.has_token and not self.is_expired(now=now)

    # ------------------------------------------------------------------
    # 协议注入 / 反向提取
    # ------------------------------------------------------------------
    def apply_to(self, envelope: GameEnvelope) -> None:
        """把令牌与 playerId 写进信封（``RootPacket`` 字段 2 与字段 8）。

        :param envelope: 将要用于发请求的信封对象（**会被就地修改**）。

        【为什么是这两个字段】
        ``player_id`` 在**登录前必须为 0**（88 字节的握手包里就没有它），
        登录后服务端要求带上 —— 少了它，业务请求会被当成"没有选定角色"而拒绝。
        """
        envelope.token = self.token
        if self.player_id:
            envelope.player_id = self.player_id
        # 令牌时间戳（服务端在 1004 下发）：战斗类请求必须回传，
        # 否则会被前置层拦成 `{"response_code":"OK"}`（见 update_from 的说明）。
        if self.time_stamp_token:
            envelope.time_stamp_token = self.time_stamp_token

    @classmethod
    def from_envelope(
        cls,
        envelope: GameEnvelope,
        *,
        account_id: int = 0,
        server_id: int = 0,
        server_name: str = "",
        player_name: str = "",
        platform_user_id: str = "",
        obtained_at: float | None = None,
        roles: tuple[dict[str, int], ...] = (),
        avatar_data: str | None = None,
    ) -> GameSessionState:
        """从已回填好的信封反向构造状态（登录流程的最后一步用）。

        :param envelope: 已经写过 ``token`` / ``player_id`` 的信封。
        :param account_id: 1002 响应里的账号 ID。
        :param server_id: 1003 里选中的区服编号。
        :param server_name: 区服名（人读用）。
        :param player_name: 角色名（人读用，如 "真昼"）。
        :param platform_user_id: 平台 userId（1003 的 ``hamiSubNoList`` 来源）。
        :param obtained_at: 取得时刻；``None`` 表示取当前时间。
        :param roles: 角色名册快照（用 :func:`role_snapshot_from` 压出来）。
            默认空元组 —— 旧调用点不受影响。
        :param avatar_data: 角色头像 base64（解析到图才传，默认 ``None``）。
        """
        return cls(
            token=envelope.token,
            player_id=envelope.player_id,
            player_name=player_name,
            time_stamp_token=envelope.time_stamp_token,
            account_id=account_id,
            server_id=server_id,
            server_name=server_name,
            platform_user_id=platform_user_id,
            obtained_at=time.time() if obtained_at is None else obtained_at,
            roles=roles,
            avatar_data=avatar_data,
        )

    # ------------------------------------------------------------------
    # 序列化（跨进程用）
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（**含令牌明文**，只用于写会话文件）。"""
        return {
            "version": SESSION_JSON_VERSION,
            "token": self.token,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "time_stamp_token": self.time_stamp_token,
            "account_id": self.account_id,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "platform_user_id": self.platform_user_id,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
            # 角色名册快照（登录 1004 的 roleInitClass 瘦身版，见字段说明）
            "roles": [dict(role) for role in self.roles],
            # 角色头像 base64（None = 还没解析到图片，前端走兜底）
            "avatar_data": self.avatar_data,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GameSessionState:
        """从字典还原（宽容策略：未知键忽略、缺失键取默认值）。

        :param data: :meth:`to_dict` 的产物，或手写的等价字典。

        【为什么宽容而不是严格】
        会话文件是"缓存"，不是协议。用户手写一个只含 ``token`` 的字典也该能用
        （环境变量那条路径本来就只给得出令牌）。真正的严格把关在发包前
        —— ``is_valid()`` 过不了就明确报错。
        """
        expires = data.get("expires_at")
        return cls(
            token=str(data.get("token") or ""),
            player_id=_as_int(data.get("player_id")),
            player_name=str(data.get("player_name") or ""),
            account_id=_as_int(data.get("account_id")),
            server_id=_as_int(data.get("server_id")),
            server_name=str(data.get("server_name") or ""),
            platform_user_id=str(data.get("platform_user_id") or ""),
            obtained_at=_as_float(data.get("obtained_at")),
            # 旧版会话文件没有这个字段 → 保持 -1（未使用），并由 session_store 给出
            # "请重新登录一次"的提示（战斗类请求需要它）。
            time_stamp_token=(
                -1 if data.get("time_stamp_token") is None else _as_int(data.get("time_stamp_token"))
            ),
            expires_at=_as_optional_positive_float(expires),
            roles=_as_role_snapshot(data.get("roles")),
            avatar_data=(str(data.get("avatar_data")) if data.get("avatar_data") else None),
        )

    # ------------------------------------------------------------------
    # 人读输出
    # ------------------------------------------------------------------
    def describe(self) -> str:
        """一行摘要（**令牌已打码**），可直接进日志/终端。"""
        where = f"{self.server_name}(#{self.server_id})" if self.server_id else "<未知区服>"
        who = f"{self.player_name} (playerId={self.player_id})" if self.player_name else f"playerId={self.player_id or '<未知>'}"
        text = (
            f"{who} @ {where}，"
            f"token={_mask_secret(self.token)}"
        )
        if self.obtained_at:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.obtained_at))
            text += f"，取得于 {stamp}"
        return text

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"GameSessionState({self.describe()})"


# ---------------------------------------------------------------------------
# 内部小工具：把 JSON 里的值转成期望类型（坏值一律当默认值，绝不抛异常）
# ---------------------------------------------------------------------------
def _as_int(value: Any) -> int:
    """尽量转成 ``int``；失败返回 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """尽量转成 ``float``；失败返回 0.0。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


#: 角色快照里保留的字段（与协议 ``RoleClass`` 的字段名一致）。
#:
#: 只留这四项：``roleSid`` 是实例 ID（占坑要填它）、``roleID`` 是 RoleMainID
#: （换算 RoleInfoID 的入口）、``roleLV`` / ``roleStar`` 是展示与条件判断要用的。
#: ``roleExp`` / ``focusState`` / ``roleSkinID`` 当前没有消费方，不进快照
#: —— 缓存"用不到的东西"只会让会话文件变大、并且多一处可能失真的数据。
ROLE_SNAPSHOT_FIELDS: Final[tuple[str, ...]] = ("roleSid", "roleID", "roleLV", "roleStar")


def role_snapshot_from(roles: Iterable[RoleClass]) -> tuple[dict[str, int], ...]:
    """把一批 ``RoleClass`` 压成会话文件里的角色快照。

    :param roles: ``LoginRes.roleInitClass`` 或 ``GetAllRoleRes.roleList``。
    :return: 只含 :data:`ROLE_SNAPSHOT_FIELDS` 的元组。

    【为什么登录与 5012 两条路都走它】两条路拿到的是**同一个 ``RoleClass``
    类型**，压出来的快照必须**逐字段一致** —— 否则"兜底"的语义就变成了
    "换了一份不一样的数据"，前端会看到两套形状。
    """
    return tuple(
        {name: int(getattr(role, name, 0) or 0) for name in ROLE_SNAPSHOT_FIELDS}
        for role in roles
    )


def _as_role_snapshot(value: Any) -> tuple[dict[str, int], ...]:
    """把 JSON 里的角色快照还原成元组（坏值一律丢弃，绝不抛异常）。

    :param value: ``to_dict()`` 写出的列表，或任何别的东西。
    :return: 只含合法条目的元组；不是列表时返回空元组。

    【为什么"坏值丢弃"而不是报错】与 :meth:`GameSessionState.from_dict` 的
    宽容策略一致：会话文件是缓存不是协议。一条脏数据不该让整份会话失效 ——
    丢掉的后果只是"这条角色看不见"，而抛异常的后果是"整个工具用不了"。
    """
    if not isinstance(value, (list, tuple)):
        return ()
    snapshot: list[dict[str, int]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        entry = {name: _as_int(item.get(name)) for name in ROLE_SNAPSHOT_FIELDS}
        if entry["roleSid"]:
            snapshot.append(entry)
    return tuple(snapshot)


def _as_optional_positive_float(value: Any) -> float | None:
    """把"可有可无的正数时间戳"转成 ``float | None``。

    :param value: JSON 里读到的原始值。
    :return: ``None``（未提供）、或读不出来、或无意义的非正数时返回 ``None``；
        否则返回该时间戳。

    【为什么读坏了要当"没有"，而不是当"已过期"】
    过期时间不是我们自己算出来的，而是对服务端信息的转述。既然**实测服务端根本
    没有下发它**（见 ``PlayerClass`` 的字段表），那么一个读不出来的值最合理的解释
    就是"这段信息本来就不存在"。把它当成"已过期"会让一次完全可用的会话被丢弃，
    而真正的失效信号来自服务端响应（``notes/open_questions.md`` 的 Q-010）。
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
