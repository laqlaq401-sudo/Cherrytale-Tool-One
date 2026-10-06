"""多账号平台令牌库（``.platform_accounts.json``）。

【它解决什么】

    python main.py run login                 # 第一次：输入账号密码，令牌被记下来
    python main.py accounts                  # 之后随时看：1 号 / 2 号 都在
    python main.py run login --account 2     # 第二次起：**选号即可，免密码**

【为什么"每账号各存一份"，而不是一个 ``.auth_token``】
``.auth_token`` 是**单值**文件：多账号会互相覆盖，表现是"我明明登过 2 号，
下次却拿着 1 号的令牌"—— 而服务端只会含糊地说令牌无效，极难定位。
这里以**平台 userId** 为键（稳定唯一），邮箱只用于展示与选择。

【安全约束（必须清楚）】
- 文件里是**明文** accessToken / refreshToken，已进 ``.gitignore`` / ``.clineignore``；
- 打印一律走 :meth:`SavedAccount.describe`（令牌**打码**，从不回显全文）；
- 最多 :data:`config.PLATFORM_ACCOUNTS_MAX`（默认 5）个账号，**新增超出直接拒绝**；
- **不保存密码**：密码只在"换令牌"那一次存在于内存。

【为什么要有上限】
令牌是**长期凭据**。一个会无限增长的凭据文件，比"最多 5 个"危险得多；
而真实场景（自己的几个号）很少超过 5 个。要更多就显式调环境变量 —— 但那是你的选择，
不是默认行为。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

import config

from client.exceptions import CherrytaleError

_LOGGER: Final[logging.Logger] = logging.getLogger("client.accounts")

#: 文件结构版本（将来字段有增减时靠它判断，而不是靠猜）
_STORE_VERSION: Final[int] = 1


class AccountStoreError(CherrytaleError):
    """账号库的**使用性**错误：超出上限、选择器没有匹配、库为空等。

    刻意继承 :class:`client.exceptions.CherrytaleError`（而不是 ``ValueError``）：
    这样 ``BaseTask.run()`` 会把它转成"失败的 TaskResult"，而不是让命令抛堆栈 ——
    "账号库满了"是要给人看的使用问题，不是代码 bug。
    错误信息本身也写成给人看的（带可选列表与下一步怎么做）。
    """


def _mask(value: str, *, visible: int = 6) -> str:
    """把凭据打码成 ``ER23f2…(40 字符)``。"""
    if not value:
        return "<空>"
    return f"{value[:visible]}…({len(value)} 字符)"


@dataclass
class SavedAccount:
    """一个已保存的账号记录。

    :param account: 平台账号（邮箱）—— 展示与选择用。
    :param user_id: 平台 userId（形如 ``ER00000000-…``）—— **账号库的键**。
    :param access_token: 访问令牌（实测 24 小时有效）。
    :param refresh_token: 续期令牌；**服务端会轮换它**，所以每次续期后必须写回。
    :param device_id: 换取令牌时用的设备标识（信息用；续期时仍取 ``config.DEVICE_ID``）。
    :param obtained_at: 取得令牌的时刻（Unix 秒）。
    :param expires_at: 过期时刻；``None`` 表示读不出 JWT 的 ``exp``。
    :param last_login_at: 最后一次成功取得令牌的时刻。
    :param game: 最近一次进区的结果（``player_id`` / ``server_id`` / ``server_name`` /
        ``account_id``）。人读用，也让"选号"时能看出这个号上次是哪个角色。
    """

    account: str = ""
    user_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    obtained_at: float = 0.0
    expires_at: float | None = None
    last_login_at: float = 0.0
    game: dict[str, Any] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 身份与有效期
    # ------------------------------------------------------------------
    @property
    def key(self) -> str:
        """账号库里的键：优先 userId（稳定唯一），退而求其次用邮箱。"""
        return self.user_id or self.account

    def expires_in_seconds(self, *, now: float | None = None) -> float | None:
        """距离过期还有多少秒；读不出 ``expires_at`` 时返回 ``None``。"""
        if self.expires_at is None:
            return None
        return self.expires_at - (time.time() if now is None else now)

    def is_expired(self, *, now: float | None = None) -> bool:
        """令牌是否已过期；读不出 ``expires_at`` 时返回 ``False``（不主动作废）。"""
        remaining = self.expires_in_seconds(now=now)
        return remaining is not None and remaining <= 0

    def can_refresh(self) -> bool:
        """是否具备"免密码续期"的条件（有 refreshToken）。"""
        return bool(self.refresh_token)

    # ------------------------------------------------------------------
    # 人读输出（**绝不打印令牌全文**）
    # ------------------------------------------------------------------
    def describe(self) -> str:
        """一行摘要：邮箱 + userId 前缀 + 令牌状态 + 上次进的角色。"""
        parts = [f"{self.account or '<无邮箱>'}  userId={_mask(self.user_id, visible=10)}"]
        remaining = self.expires_in_seconds()
        if remaining is None:
            parts.append("令牌剩余 <未知>")
        elif remaining <= 0:
            parts.append("令牌已过期" + ("（会自动续期）" if self.can_refresh() else "（无 refreshToken，需重新输入密码）"))
        else:
            parts.append(f"令牌剩余 {int(remaining // 60)} 分钟")
        server_id = self.game.get("server_id")
        if server_id:
            role = self.game.get("player_name") or f"角色 {self.game.get('player_id')}"
            parts.append(
                f"上次：#{server_id} {self.game.get('server_name')}"
                f"（{role}）"
            )
        return "  ".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"SavedAccount({self.describe()})"

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（**含明文令牌**，只用于写账号库）。"""
        return {
            "account": self.account,
            "user_id": self.user_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
            "last_login_at": self.last_login_at,
            "game": dict(self.game),
            "sessions": {str(k): dict(v) for k, v in self.sessions.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SavedAccount:
        """从字典还原（宽容：未知键忽略、缺失键取默认值、坏值不抛异常）。

        账号库是**缓存**，不是协议：手写一个只含 ``access_token`` 的条目也该能用。
        真正的严格把关在"要不要拿它去发包"那一步（见 :meth:`AccountStore.refresh_if_needed`
        与 ``PlatformClient``）。
        """
        expires = data.get("expires_at")
        game = data.get("game")
        game_dict = dict(game) if isinstance(game, Mapping) else {}

        sessions_raw = data.get("sessions")
        sessions: dict[str, dict[str, Any]] = {}
        if isinstance(sessions_raw, Mapping):
            for sid, sval in sessions_raw.items():
                if isinstance(sval, Mapping):
                    sessions[str(sid)] = dict(sval)
        # 兼容旧版本：若 sessions 为空但 game 存在，则平滑升级转入 sessions
        if not sessions and game_dict.get("server_id"):
            sessions[str(game_dict["server_id"])] = dict(game_dict)

        # 若 sessions 里的条目缺少 token，但当前 .session.json 正好匹配该账号与区服，则补齐 token
        try:
            from client.session_store import load_session_state
            active_s = load_session_state()
            if active_s and active_s.has_token and active_s.platform_user_id == data.get("user_id"):
                sid_k = str(active_s.server_id)
                if sid_k in sessions and not sessions[sid_k].get("token"):
                    sessions[sid_k]["token"] = active_s.token
                    sessions[sid_k]["time_stamp_token"] = active_s.time_stamp_token
                    sessions[sid_k]["obtained_at"] = active_s.obtained_at
        except Exception:
            pass

        return cls(
            account=str(data.get("account") or ""),
            user_id=str(data.get("user_id") or ""),
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            device_id=str(data.get("device_id") or ""),
            obtained_at=_as_float(data.get("obtained_at")),
            expires_at=None if expires is None else _as_optional_float(expires),
            last_login_at=_as_float(data.get("last_login_at")),
            game=game_dict,
            sessions=sessions,
        )


class AccountStore:
    """账号库的读写与选择（**所有落盘都经过这里**）。"""

    def __init__(self, path: Path | None = None, *, max_accounts: int | None = None) -> None:
        self.path = Path(path) if path is not None else config.PLATFORM_ACCOUNTS_FILE
        self.max_accounts = (
            config.PLATFORM_ACCOUNTS_MAX if max_accounts is None else max_accounts
        )

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> dict[str, SavedAccount]:
        """读全部账号（``键 → 记录``）。

        :raises AccountStoreError: 文件损坏、读不了时。

        **为什么损坏要报错而不是当作空库**：静默当成空库，会让人以为"登录记录丢了"，
        然后在不知情的情况下重新登录、覆盖掉可能还能用的凭据。
        """
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AccountStoreError(
                f"账号库读取失败：{self.path}\n  原因：{exc}\n"
                "  处理：删掉该文件再登录一次即可（它是缓存，不含服务端的唯一数据）"
            ) from exc

        records = data.get("accounts") if isinstance(data, Mapping) else None
        if not isinstance(records, Mapping):
            return {}

        version = data.get("version") if isinstance(data, Mapping) else None
        if version is not None and version != _STORE_VERSION:
            _LOGGER.debug(
                "账号库结构版本 %s（当前 %s），按宽容策略继续解析", version, _STORE_VERSION
            )

        accounts: dict[str, SavedAccount] = {}
        for key, value in records.items():
            if isinstance(value, Mapping):
                accounts[str(key)] = SavedAccount.from_dict(value)
        return accounts

    def save(self, accounts: Mapping[str, SavedAccount]) -> Path:
        """写回账号库（含令牌明文；该文件已在两份忽略清单里）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "accounts": {key: item.to_dict() for key, item in accounts.items()},
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(text, encoding="utf-8")
        return self.path

    # ------------------------------------------------------------------
    # 增删改查
    # ------------------------------------------------------------------
    def list_accounts(self) -> list[SavedAccount]:
        """按**插入顺序**返回所有账号。

        顺序稳定 ⇒ ``--account 1`` / ``--account 2`` 每次都指同一个号
        （新号排在最后），这比"按时间排序"更符合"用序号选号"的直觉。
        """
        return list(self.load().values())

    def upsert(self, account: SavedAccount) -> SavedAccount:
        """新增或更新一个账号记录（更新已有账号**不受上限限制**）。

        :raises AccountStoreError: 缺少唯一键，或**新增**时超出上限。
        """
        key = account.key
        if not key:
            raise AccountStoreError("账号记录既没有 userId 也没有邮箱，无法作为唯一键")
        accounts = self.load()
        if key not in accounts and len(accounts) >= self.max_accounts:
            raise AccountStoreError(
                f"账号库已满（{len(accounts)}/{self.max_accounts}），"
                f"无法新增 {account.account or key}。\n"
                "  处理：用 `python main.py accounts` 看看有哪些，把不再用的删掉"
                "（或调 CHERRYTALE_ACCOUNTS_MAX —— 但令牌是长期凭据，请谨慎）。"
            )
        accounts[key] = account
        self.save(accounts)
        _LOGGER.debug("账号库已更新：%s", account.describe())
        return account

    def remove(self, selector: str) -> SavedAccount:
        """按选择器删除一个账号，返回被删的记录。

        :raises AccountStoreError: 没有匹配项时。
        """
        target = self.pick(selector)
        accounts = self.load()
        accounts.pop(target.key, None)
        self.save(accounts)
        _LOGGER.info("已从账号库删除：%s", target.describe())
        return target

    # ------------------------------------------------------------------
    # 选择
    # ------------------------------------------------------------------
    def describe_all(self) -> list[str]:
        """给 ``main.py accounts`` 用的多行摘要（带序号；令牌已打码）。"""
        accounts = self.list_accounts()
        if not accounts:
            return [
                "（账号库为空：先跑一次带账号密码的登录，例如 python main.py run login）"
            ]
        return [f"{index}) {item.describe()}" for index, item in enumerate(accounts, 1)]

    def pick(self, selector: str | None = None) -> SavedAccount:
        """按**序号**（``1``）/ **邮箱** / **userId** 选一个账号。

        :param selector: 选择器；``None``/空表示"库里只有一个号就自动选它"。
        :raises AccountStoreError: 库为空、有多个却没指定、或没有匹配
            （错误信息里总带着**可选列表**，不让调用方去猜）。
        """
        accounts = self.list_accounts()
        if not accounts:
            raise AccountStoreError(
                "账号库里还没有任何账号。\n"
                "  先用账号密码登录一次，令牌就会被记下来：python main.py run login"
            )
        chosen = (selector or "").strip()
        if not chosen:
            if len(accounts) == 1:
                return accounts[0]
            raise AccountStoreError(
                "账号库里不止一个账号，请指定用哪个：\n"
                "  --account <序号|邮箱>（或环境变量 CHERRYTALE_ACCOUNT=...）\n"
                + _indent_lines(self.describe_all())
            )
        if chosen.isdigit():
            index = int(chosen)
            if 1 <= index <= len(accounts):
                return accounts[index - 1]
            raise AccountStoreError(
                f"序号 {index} 超出范围（1~{len(accounts)}）：\n"
                + _indent_lines(self.describe_all())
            )
        for item in accounts:
            if chosen.lower() in (item.account.lower(), item.user_id.lower()):
                return item
        raise AccountStoreError(
            f"没有匹配的账号：{chosen}\n" + _indent_lines(self.describe_all())
        )

    def find_account(self, selector: str | None) -> SavedAccount | None:
        """根据序号、邮箱或 userId 查找账号，找不到时返回 None 而非抛异常。"""
        if not selector:
            return None
        try:
            return self.pick(selector)
        except AccountStoreError:
            pass
        for item in self.list_accounts():
            if selector.lower() in (item.account.lower(), item.user_id.lower()):
                return item
        return None

    # ------------------------------------------------------------------
    # 续期（免密码的关键）
    # ------------------------------------------------------------------
    def refresh_if_needed(
        self, account: SavedAccount, *, leeway: float = 300.0
    ) -> tuple[SavedAccount, bool]:
        """令牌快过期时自动续期（**不需要验证码**），并把新令牌写回账号库。

        :param account: 账号库里的记录。
        :param leeway: 提前多少秒算"快过期"（默认 5 分钟，足够覆盖一次进区耗时）。
        :return: ``(记录, 是否真的续期过)``。

        【为什么必须写回 refresh_token】
        服务端**会轮换** refreshToken。只更新 accessToken 而丢掉新的 refreshToken，
        下一次续期就会失败 —— 现象是"明明刚续过却又要密码"，极难定位。

        .. note::
           续期**不做重试**（``PlatformClient`` 的 POST 不自动重试）：续期失败时
           更该做的是重新输一次密码，而不是反复敲同一个失效的 refreshToken。
        """
        remaining = account.expires_in_seconds()
        if remaining is not None and remaining > leeway:
            return account, False
        if not account.can_refresh():
            _LOGGER.info(
                "账号 %s 的令牌即将过期或已过期，但没有 refreshToken —— 需要重新输入密码",
                account.account,
            )
            return account, False

        from client.platform_client import PlatformClient  # 延迟导入：避免模块级循环

        client = PlatformClient(token=account.access_token)
        try:
            token = client.refresh_access_token(account.refresh_token)
        finally:
            client.close()

        updated = dataclasses.replace(
            account,
            access_token=token.access_token,
            refresh_token=token.refresh_token or account.refresh_token,
            obtained_at=token.obtained_at,
            expires_at=token.expires_at(),
            last_login_at=time.time(),
        )
        self.upsert(updated)
        _LOGGER.info("已自动续期：%s", updated.describe())
        return updated, True


# ---------------------------------------------------------------------------
# 内部小工具（坏值一律当"没有"，绝不因为一个坏字段炸掉整个账号库）
# ---------------------------------------------------------------------------
def _indent_lines(lines: list[str]) -> str:
    """把多行摘要缩进两格（拼进异常信息时看着更整齐）。"""
    return "\n".join(f"  {line}" for line in lines)


def _as_float(value: Any) -> float:
    """尽量转成 ``float``；失败返回 0.0。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_optional_float(value: Any) -> float | None:
    """尽量转成正数 ``float``；``None``/读不出/非正数一律返回 ``None``。

    与 ``models/session_state.py`` 同一个原则：坏掉的"过期时间"当**没有**处理，
    而不是"当作已过期"—— 后者会让一个本来能用的令牌被无谓丢弃。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None