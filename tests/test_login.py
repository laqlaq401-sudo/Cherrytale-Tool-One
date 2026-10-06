"""登录模块（``tasks/login.py``）的单元测试 —— 离线全链路 + 可选联网。

运行方式：

    python -m pytest tests/test_login.py -q                          # 全离线
    CHERRYTALE_RUN_NETWORK_TESTS=1 python -m pytest tests/test_login.py -q
    CHERRYTALE_RUN_LOGIN_NETWORK_TESTS=1 + CHERRYTALE_RUN_NETWORK_TESTS=1 ...

【离线全链路是怎么做到的】
真实登录要按顺序发 3 次请求（99015 / 1001 / 1003）并先问平台要 userId。
测试当然不能联网，于是：

1. 把 ``GameClient.post_raw`` 换成"按**请求消息号**回放响应字节"的替身 ——
   这是在**唯一的网络出口**上做替换，所以组装、vCode 计算、信封解析、
   会话回填、异常包判定等真实代码全都会被跑到；
2. 把 ``tasks.login.PlatformClient`` 换成假的（固定返回一个 userId）。

【响应字节的来源：为什么要"合成"而不是直接用抓包】
- 99016 握手：用**真实抓包**（``protocol_samples/gateway_samples.py``）；
- 1002 / 1004：**合成**。两个理由：
  1. 1004 的完整响应只存在于本地抓包文件里，直接引用会把**真实令牌带进仓库**
     —— 凭据不进版本库是硬规则（本文件里的令牌是假值，长度仍照 34 字符，
     这样才检验得了"长度不匹配就报错"之类的判断）；
  2. 这条用例要验证的是**管道**（令牌：子包 → 会话状态 → 信封 → 会话文件），
     结构正确的合成字节足以证明；真实字节的覆盖交给下面的联网用例。

【联网用例分两层开关】
- ``CHERRYTALE_RUN_NETWORK_TESTS=1``：只跑"握手 + 1001"（**只读**，不产生账号数据）；
- 再加 ``CHERRYTALE_RUN_LOGIN_NETWORK_TESTS=1``：才跑完整登录（含 1003 进区，
  可能有副作用：登录奖励结算、风控计数）。默认跳过。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

import config
from client.account_store import AccountStore, SavedAccount
from client.exceptions import AuthenticationError
from client.game_client import GameClient
from client.platform_client import PlatformClient
from client.session import GameSession
from client.session_store import load_session_state
from models.mail import MailGetListPacket
from models.packet_ids import packet_name
from models.protobuf_wire import encode_int, encode_message, encode_string
from models.server import AccountClientInfoLoginPacket, AccountClientInfoLoginRes
from models.session_state import GameSessionState
from tasks.base import BaseTask, TaskResult, create_task, register_task
from tests.test_game_client import FakeResponse, handshake_response_bytes
from tests.test_game_error import exception_response_bytes
from tests.test_auth_token import (
    CAPTCHA_SUCCESS,
    LOGIN_SUCCESS,
    install_transport,
    make_response,
)
# ⚠️ 名字要与本文件的"游戏侧"常量区分开：
#   本文件顶部的 FAKE_TOKEN / FAKE_USER_ID 是**游戏**令牌与占位 userId，
#   下面这几个才是**平台**登录响应里的值（同名不同物，混用会让断言看着"失败"）。
from tests.test_auth_token import FAKE_REFRESH as PLATFORM_REFRESH_TOKEN
from tests.test_auth_token import FAKE_TOKEN as PLATFORM_ACCESS_TOKEN
from tests.test_auth_token import FAKE_USER_ID as PLATFORM_USER_ID

import tasks.login as login_task

# ---------------------------------------------------------------------------
# 假凭据与假响应字节（**全部是假值**，只有长度照实测）
# ---------------------------------------------------------------------------
FAKE_TOKEN = "FAKETOKEN00000000000000000000000000"  # 34 字符，与实测一致
FAKE_PLAYER_ID = 10000001
FAKE_ACCOUNT_ID = 10000004
FAKE_SERVER_ID = 167
FAKE_SERVER_NAME = "宮本武藏"
FAKE_USER_ID = "ER00000000-0000-0000-0000-000000000000"  # 38 字符，与真实 userId 等长

# ★ 2026-10-06：登录链固定多发的"登录签到"空查询（33055 → 33056，
# 见 tasks/login_signin.py）—— 顺序断言与响应路由表都把它算在内。
PACKET_LOGIN_SIGNIN = 33055
PACKET_LOGIN_SIGNIN_RES = 33056


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼一段响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def server_bytes(
    server_id: int = FAKE_SERVER_ID,
    name: str = FAKE_SERVER_NAME,
    *,
    player_info: str = "FAKE_ROLE",  # 非空 = 本账号在该区**已有角色**
) -> bytes:
    """``ServerListClass``：1 serverID、2 serverName、3 serverState、4 serverNote、
    5 people_count、6 playerInfo、7 serverGroupID。"""
    return (
        encode_int(1, server_id)
        + encode_string(2, name)
        + encode_int(3, 0)
        + encode_string(4, "")
        + encode_int(5, 0)
        + encode_string(6, player_info)
        + encode_int(7, 1)
    )


def account_login_res_bytes(*, error_code: int = 0, with_character: bool = True) -> bytes:
    """合成 1002 响应：``errorCode``(1) + ``accountID``(2) + 区服列表(3/4)。"""
    payload = (
        encode_int(1, error_code)
        + encode_int(2, FAKE_ACCOUNT_ID)
        + encode_message(3, server_bytes(player_info="FAKE_ROLE" if with_character else ""))
        + encode_message(4, server_bytes(player_info="FAKE_ROLE" if with_character else ""))
    )
    return envelope_bytes(1002, payload)


def login_res_bytes(
    *, token: str = FAKE_TOKEN, player_id: int = FAKE_PLAYER_ID, error_code: int = 0
) -> bytes:
    """合成 1004 响应：``LoginRes{errorCode(1), playerInitClass(2: PlayerClass)}``。"""
    player = encode_string(1, token) + encode_int(2, player_id)
    return envelope_bytes(1004, encode_int(1, error_code) + encode_message(2, player))


# ---------------------------------------------------------------------------
# 替身：平台客户端与"按包号回放"的网络出口
# ---------------------------------------------------------------------------
class FakeUserInfo:
    """平台用户信息的最小替身（登录只用到 ``user_id``，其余属性只为日志存在）。"""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.coins = 0


class FakePlatformClient:
    """``PlatformClient`` 的替身：不发任何 HTTP 请求。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "FakePlatformClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get_user_info(self) -> FakeUserInfo:
        return FakeUserInfo(FAKE_USER_ID)


def route_responses(
    monkeypatch: pytest.MonkeyPatch, responses: dict[int, bytes]
) -> list[int]:
    """把 ``GameClient.post_raw`` 换成"按请求消息号回放响应"的替身。

    :param responses: ``{请求消息号: 响应字节}``。
    :return: 记录请求消息号的列表（用例靠它断言"到底发了哪些包、顺序对不对"）。

    替身在**唯一的网络出口**上，因此它拦下的那一刻，请求字节已经被真实组装完了
    —— 这也是为什么这份用例能覆盖 vCode、信封编码、会话注入这些细节。
    """
    sent: list[int] = []

    def fake_post_raw(self: GameClient, body: bytes) -> Any:
        packet_id = GameClient._packet_id_of(body)
        assert packet_id is not None, "请求体里读不出消息号 —— 组装阶段就出错了"
        sent.append(packet_id)
        payload = responses.get(packet_id)
        assert payload is not None, (
            f"替身没有准备 {packet_id}（{packet_name(packet_id)}）的响应"
        )
        return FakeResponse(payload)

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return sent


def offline_session() -> GameSession:
    """不会联网的会话（域名是保留域名）。"""
    return GameSession("https://example.invalid", timeout=1.0)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def isolated_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把会话文件指向临时目录 —— **绝不碰项目根目录那份真实 ``.session.json``**。

    这是本文件最容易被忽略、后果却最严重的一点：登录用例会真的写会话文件，
    万一写到项目里，就会覆盖你正在用的会话（甚至把真实令牌留在磁盘上）。
    """
    target = tmp_path / "session.json"
    monkeypatch.setenv(config.SESSION_FILE_ENV_NAME, str(target))
    monkeypatch.delenv(config.SESSION_TOKEN_ENV_NAME, raising=False)
    monkeypatch.delenv(config.GAME_PLAYER_ID_ENV_NAME, raising=False)
    return target


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> GameSession:
    """离线会话：域名是保留域名；选服配置清空，让结果与本地环境无关。

    .. note::
       这里把 ``config.AUTH_TOKEN`` 钉成一个假值 —— 既有用例走的是
       "**显式令牌**"那条分支（优先级①）。不钉住的话，结果会取决于
       "这台机器上有没有 ``.auth_token``"：有就走①、没有就落到账号库分支②~⑤，
       同一个用例在别人机器上可能失败。**测试必须与开发机的文件状态无关。**
       账号库路径也一并指到临时目录，绝不动项目根目录那份真实记录。
    """
    monkeypatch.setattr(config, "SERVER_ID", None)
    monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
    monkeypatch.setattr(config, "SESSION_SAVE_ENABLED", True)
    monkeypatch.setattr(config, "AUTH_TOKEN", "FAKE-PLATFORM-TOKEN")
    monkeypatch.setattr(config, "PLATFORM_ACCOUNTS_FILE", tmp_path / "accounts.json")
    # 登录签到钩子的记账文件也必须指到临时目录：登录用例会真的走完这条链，
    # 绝不能把"今天已签到"写进项目根目录的真实记账。
    monkeypatch.setattr(config, "LOGIN_SIGNIN_STATE_FILE", tmp_path / "login_signin.json")
    monkeypatch.setattr(login_task, "PlatformClient", FakePlatformClient)
    return GameSession("https://example.invalid", timeout=1.0)


def full_login_responses() -> dict[int, bytes]:
    """四条请求各自的响应：99015 握手（真实抓包）、1001、1003（合成）、33056（空）。

    ★ 2026-10-06：登录成功后登录任务会固定发一次登录签到空查询（33055），
    所以响应路由表必须备好 33056 —— 否则钩子拿不到响应，每条登录用例
    都会在开发者日志里多出一条 ✘（虽然不影响登录结果）。
    """
    return {
        99015: handshake_response_bytes(),
        1001: account_login_res_bytes(),
        1003: login_res_bytes(),
        PACKET_LOGIN_SIGNIN: envelope_bytes(PACKET_LOGIN_SIGNIN_RES, b""),
    }


# ---------------------------------------------------------------------------
# 离线全链路
# ---------------------------------------------------------------------------
class TestOfflineFullChain:
    """1001 → 1002 → 选服 → 1003 → 1004，以及令牌的去向。"""

    def test_packet_order_matches_design(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """顺序必须是"先握手、再登录、最后进区"（服务端靠这个顺序建会话）。

        ★ 2026-10-06：进区成功后还固定多发一次登录签到空查询（33055）——
        它跑在会话就绪之后，所以排在 1003 后面。
        """
        sent = route_responses(monkeypatch, full_login_responses())
        result = create_task("login", session).run()
        assert result.ok, result.message
        assert sent == [99015, 1001, 1003, PACKET_LOGIN_SIGNIN]

    def test_login_signin_hook_marks_bookkeeping(
        self,
        session: GameSession,
        isolated_session_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """登录成功 → 钩子已把"今天"写进记账文件，并留一条 ✔ 摘要日志。"""
        import json as _json
        import logging as _logging

        from tasks.login_signin import TITLE

        route_responses(monkeypatch, full_login_responses())
        with caplog.at_level(_logging.INFO, logger="tasks.login_signin"):
            result = create_task("login", session).run()

        assert result.ok, result.message
        state_file = tmp_path / "login_signin.json"
        payload = _json.loads(state_file.read_text(encoding="utf-8"))
        entry = payload["targets"][f"s={FAKE_SERVER_ID}|p={FAKE_PLAYER_ID}"]
        assert entry["date"] == time.strftime("%Y-%m-%d")
        summary = [r.message for r in caplog.records if TITLE in r.message]
        assert summary, "开发者日志里必须有一条登录签到的摘要行"
        assert "✔" in summary[-1]

    def test_session_state_is_filled(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """令牌 / playerId / 区服 / 账号都要落到 ``session.game_state`` 上。"""
        route_responses(monkeypatch, full_login_responses())
        result = create_task("login", session).run()

        state = session.require_game_state()
        assert (state.token, state.player_id) == (FAKE_TOKEN, FAKE_PLAYER_ID)
        assert (state.account_id, state.server_id) == (FAKE_ACCOUNT_ID, FAKE_SERVER_ID)
        assert state.server_name == FAKE_SERVER_NAME
        assert state.platform_user_id == FAKE_USER_ID
        assert f"playerId={FAKE_PLAYER_ID}" in result.message

    def test_session_file_is_written_and_reloadable(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨进程复用的关键：文件里真的写下了可用会话（否则下一条命令还得手工带令牌）。"""
        route_responses(monkeypatch, full_login_responses())
        result = create_task("login", session).run()

        assert result.data["session_saved"] is True
        reloaded = load_session_state()
        assert reloaded is not None
        assert reloaded.token == FAKE_TOKEN
        assert reloaded.player_id == FAKE_PLAYER_ID

        raw = json.loads(isolated_session_file.read_text(encoding="utf-8"))
        assert raw["token"] == FAKE_TOKEN
        assert raw["server_id"] == FAKE_SERVER_ID

    def test_result_data_never_contains_plaintext_token(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TaskResult`` 会被打印到终端、也可能被贴进日志 —— 里面不能有令牌明文。"""
        route_responses(monkeypatch, full_login_responses())
        result = create_task("login", session).run()
        assert FAKE_TOKEN not in json.dumps(result.data, ensure_ascii=False, default=str)
        assert FAKE_TOKEN not in result.message

    def test_token_reaches_the_next_request_bytes(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """整条改造的意义所在：登录之后，业务请求的信封里**确实带上了令牌**。"""
        route_responses(monkeypatch, full_login_responses())
        create_task("login", session).run()

        with GameClient.for_session(session) as game:
            body = game.build(MailGetListPacket()).body
            assert FAKE_TOKEN.encode() in body, "信封里没带上令牌（字段 2）"
            assert game.envelope.player_id == FAKE_PLAYER_ID

    def test_abnormal_packet_is_reported_without_retry_hint_noise(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务端回 99004(-3) 时，报错必须说清"不能靠重试解决"。"""
        route_responses(
            monkeypatch,
            {99015: handshake_response_bytes(), 1001: exception_response_bytes()},
        )
        result = create_task("login", session).run()
        assert result.ok is False
        assert "99004" in result.message
        assert "重试" in result.message

    def test_1004_error_code_is_reported_as_such(
        self, session: GameSession, isolated_session_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1004 的 ``errorCode != 0`` 必须报成"进区被拒"，而不是含糊的"字段缺失"。"""
        responses = full_login_responses()
        # 消息号做不出关键字参数（``1003=...`` 不是合法语法），所以用字典覆盖
        responses[1003] = login_res_bytes(error_code=7, token="", player_id=0)
        route_responses(monkeypatch, responses)
        result = create_task("login", session).run()
        assert result.ok is False
        assert "1004" in result.message
        assert session.game_state is None


class TestDryRun:
    """演练模式：**一个字节都不发**（承诺了就得守住）。"""

    def test_dry_run_never_calls_platform_or_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("演练模式不该走到这里（既不该问平台，也不该发包）")

        monkeypatch.setattr(login_task, "PlatformClient", forbidden)
        monkeypatch.setattr(GameClient, "post_raw", forbidden)
        session = GameSession("https://example.invalid", timeout=1.0)

        result = create_task("login", session, dry_run=True).run()
        assert result.ok is True
        assert result.data["body_size"] == 330  # 与实测的 1001 请求体等长
        assert session.game_state is None


# ---------------------------------------------------------------------------
# 会话状态与"能不能发包"的关系
# ---------------------------------------------------------------------------
@register_task
class _OpenClientProbeTask(BaseTask):
    """测试用任务：只验证 ``open_game_client()`` 给不给客户端、给的是不是带令牌的。"""

    name = "test_open_client_probe"
    requires_auth = True

    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            return TaskResult(
                task=self.name, ok=True, data={"has_token": bool(game.envelope.token)}
            )


class TestOpenGameClient:
    """``BaseTask.open_game_client()``：把"没令牌就发不出去"变成硬保证。"""

    def test_without_state_refuses_to_run(self) -> None:
        """只有平台令牌 → **拒绝**构造客户端（这是本次改造的核心保证）。"""
        session = offline_session()
        session.token_store.update(uid="u1", token="tok")
        result = create_task("test_open_client_probe", session).run()
        assert result.ok is False
        assert "会话状态" in result.message

    def test_dry_run_allows_assembling_without_token(self) -> None:
        """演练模式放行：它只组装、不发送，而且要看的就是"没令牌时字节长什么样"。"""
        session = offline_session()
        session.token_store.update(uid="u1", token="tok")
        result = create_task("test_open_client_probe", session, dry_run=True).run()
        assert result.ok is True
        assert result.data["has_token"] is False

    def test_with_state_the_client_carries_the_token(self) -> None:
        session = offline_session()
        session.set_game_state(GameSessionState(token="TOK", player_id=7))
        result = create_task("test_open_client_probe", session).run()
        assert result.ok is True
        assert result.data["has_token"] is True

    def test_for_session_injects_state(self) -> None:
        """``GameClient.for_session`` 就是 helper 用的那条路（同一套注入逻辑）。"""
        session = offline_session()
        session.set_game_state(GameSessionState(token="TOK", player_id=7))
        with GameClient.for_session(session) as game:
            assert game.envelope.token == "TOK"
            assert game.envelope.player_id == 7

    def test_for_session_requires_state(self) -> None:
        with pytest.raises(AuthenticationError):
            GameClient.for_session(offline_session())


class TestTaskDiscipline:
    """静态约定：业务任务一律走 ``open_game_client()``（防止以后新任务又漏带令牌）。"""

    #: 允许直接构造 ``GameClient`` 的文件（各有明确理由）
    ALLOWED = {
        "base.py",    # helper 自身的实现
        "login.py",   # 会话状态的**来源**：此刻还没有令牌可注入
        "probe.py",   # 免登录的握手任务：本就不该带令牌
        # 登录签到的内置钩子：与 login.py 同理 —— 它跑在"会话刚就绪"的时刻，
        # 自建客户端后用 GameSessionState.apply_to 注入令牌（与 webapi 直连探活
        # 同一模式）；它不是 BaseTask，没有 self.open_game_client() 可用。
        "login_signin.py",
    }

    def test_tasks_do_not_build_game_client_directly(self) -> None:
        offenders: list[str] = []
        for path in sorted((config.PROJECT_ROOT / "tasks").glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"\bGameClient\(", stripped):
                    offenders.append(f"{path.name}:{number}: {stripped}")
        assert offenders == [], (
            "业务任务必须用 self.open_game_client()（它会注入 1004 令牌）；"
            "直接 new GameClient 会让请求不带令牌，而服务端只会回含糊的「包异常」：\n"
            + "\n".join(offenders)
        )


# ---------------------------------------------------------------------------
# 平台身份来源的四级优先级（全离线）
# ---------------------------------------------------------------------------
class TestPlatformIdentityResolution:
    """``run login`` 到底用哪个平台账号：①显式令牌 → ②选号 → ③账号密码 → ④唯一账号。

    这四条分支是"多账号 + 免密码"的核心，所以**每条都要有用例**；
    其中②③还会顺带验证"进区结果被写回账号库"。
    """

    @staticmethod
    def prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> GameSession:
        """离线会话：清空"显式令牌"（模拟没有 .auth_token 的机器），账号库指向临时文件。"""
        monkeypatch.setattr(config, "SERVER_ID", None)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        monkeypatch.setattr(config, "SESSION_SAVE_ENABLED", True)
        monkeypatch.setattr(config, "AUTH_TOKEN", "")
        monkeypatch.setattr(config, "PLATFORM_ACCOUNTS_FILE", tmp_path / "accounts.json")
        monkeypatch.delenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, raising=False)
        monkeypatch.delenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, raising=False)
        monkeypatch.delenv(config.PLATFORM_ACCOUNT_ENV_NAME, raising=False)
        return GameSession("https://example.invalid", timeout=1.0)

    @staticmethod
    def save_account(
        tmp_path: Path,
        *,
        email: str = "saved@example.com",
        user_id: str = FAKE_USER_ID,
        token: str = "saved-access-token",
        refresh: str = "saved-refresh-token",
        expires_in: float = 3600.0,
    ) -> SavedAccount:
        """往临时账号库里写一个账号。"""
        account = SavedAccount(
            account=email,
            user_id=user_id,
            access_token=token,
            refresh_token=refresh,
            device_id="device-1",
            obtained_at=time.time(),
            expires_at=time.time() + expires_in,
            last_login_at=time.time(),
        )
        AccountStore(tmp_path / "accounts.json").upsert(account)
        return account

    def test_selected_account_skips_network_and_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """② 选号：**不发任何平台请求**（连替身都没准备响应），直接进区。"""
        session = self.prepare(monkeypatch, tmp_path)
        self.save_account(tmp_path)
        install_transport(monkeypatch)  # 一个响应都不给：一旦真发请求就会炸
        route_responses(monkeypatch, full_login_responses())

        result = create_task("login", session, account="1").run()

        assert result.ok, result.message
        assert result.data["user_id"] == FAKE_USER_ID
        assert result.data["player_id"] == FAKE_PLAYER_ID

    def test_progress_is_written_back_to_the_account(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """进区成功后把角色信息写回账号库（选号时能看出上次是哪个角色）。"""
        session = self.prepare(monkeypatch, tmp_path)
        self.save_account(tmp_path)
        install_transport(monkeypatch)
        route_responses(monkeypatch, full_login_responses())

        create_task("login", session, account="1").run()

        saved = AccountStore(tmp_path / "accounts.json").pick("1")
        assert saved.game["player_id"] == FAKE_PLAYER_ID
        assert saved.game["server_id"] == FAKE_SERVER_ID

    def test_expired_saved_account_is_refreshed_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """② 的续期分支：令牌只剩 60 秒 → 先续期（用替身），再用新令牌进区。"""
        session = self.prepare(monkeypatch, tmp_path)
        self.save_account(tmp_path, token="stale-token", expires_in=60)
        install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        route_responses(monkeypatch, full_login_responses())

        result = create_task("login", session, account="1").run()

        assert result.ok, result.message
        persisted = AccountStore(tmp_path / "accounts.json").pick("1")
        assert persisted.access_token == PLATFORM_ACCESS_TOKEN, "续期后的新令牌应当被写回"

    def test_password_credentials_are_exchanged_and_saved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """③ 账号密码：换成令牌 → 自动入库 → 继续进区（不用先手动导出令牌）。"""
        session = self.prepare(monkeypatch, tmp_path)
        monkeypatch.setenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, "fresh@example.com")
        monkeypatch.setenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, "secret")
        install_transport(
            monkeypatch, make_response(CAPTCHA_SUCCESS), make_response(LOGIN_SUCCESS)
        )
        route_responses(monkeypatch, full_login_responses())

        result = create_task("login", session).run()

        assert result.ok, result.message
        saved = AccountStore(tmp_path / "accounts.json").pick("1")
        assert saved.account == "fresh@example.com"
        assert saved.user_id == PLATFORM_USER_ID
        assert saved.refresh_token == PLATFORM_REFRESH_TOKEN

    def test_selector_wins_over_legacy_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``.auth_token`` 之类的旧令牌**不该**压过显式选号。

        这条守着一个很容易踩的坑：根目录留着上次的 ``.auth_token``，于是
        ``--account 2`` 被静默忽略、程序拿着 1 号的令牌进区 ——
        现象只是"令牌无效"，完全看不出"选号没生效"。
        """
        session = self.prepare(monkeypatch, tmp_path)
        monkeypatch.setattr(config, "AUTH_TOKEN", "LEGACY-TOKEN-OF-ANOTHER-ACCOUNT")
        self.save_account(tmp_path, user_id="ER-SELECTED", expires_in=3600)
        install_transport(monkeypatch)  # 一个响应都不给：一旦走令牌分支就会炸
        route_responses(monkeypatch, full_login_responses())

        result = create_task("login", session, account="1").run()

        assert result.ok, result.message
        assert result.data["user_id"] == "ER-SELECTED"

    def test_single_saved_account_is_used_automatically(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """④ 库里只有一个号 → 连选择器都不用给。"""
        session = self.prepare(monkeypatch, tmp_path)
        self.save_account(tmp_path)
        install_transport(monkeypatch)
        route_responses(monkeypatch, full_login_responses())

        result = create_task("login", session).run()

        assert result.ok, result.message
        assert result.data["user_id"] == FAKE_USER_ID

    def test_multiple_accounts_without_choice_asks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⑤ 多个号却没指定 → 必须**问**，不能自己挑一个（挑错 = 进错账号）。"""
        session = self.prepare(monkeypatch, tmp_path)
        self.save_account(tmp_path, email="one@example.com", user_id="ER-1")
        self.save_account(tmp_path, email="two@example.com", user_id="ER-2")
        install_transport(monkeypatch)

        result = create_task("login", session).run()

        assert result.ok is False
        assert "无法确定要用哪个平台账号" in result.message
        assert "one@example.com" in result.message and "two@example.com" in result.message

    def test_nothing_available_explains_how_to_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⑤ 什么都没有 → 报错要写清"下一步怎么做"（新人最容易卡在这里）。"""
        session = self.prepare(monkeypatch, tmp_path)
        install_transport(monkeypatch)

        result = create_task("login", session).run()

        assert result.ok is False
        assert "无法确定要用哪个平台账号" in result.message
        assert config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME in result.message
        assert "--account" in result.message


# ---------------------------------------------------------------------------
# 联网用例（默认全跳过；两层开关见模块文档）
# ---------------------------------------------------------------------------
NETWORK_TESTS_ENABLED = os.getenv("CHERRYTALE_RUN_NETWORK_TESTS", "0") == "1"
LOGIN_NETWORK_TESTS_ENABLED = os.getenv("CHERRYTALE_RUN_LOGIN_NETWORK_TESTS", "0") == "1"

requires_network = pytest.mark.skipif(
    not NETWORK_TESTS_ENABLED,
    reason="默认不发起真实网络请求；需要时设 CHERRYTALE_RUN_NETWORK_TESTS=1",
)
requires_full_login = pytest.mark.skipif(
    not (NETWORK_TESTS_ENABLED and LOGIN_NETWORK_TESTS_ENABLED),
    reason="完整登录会真的进区（可能有副作用）；确认后再设 CHERRYTALE_RUN_LOGIN_NETWORK_TESTS=1",
)
has_platform_token = pytest.mark.skipif(
    not config.has_auth_token(),
    reason="缺少平台令牌；需要时设 CHERRYTALE_AUTH_TOKEN 或写 .auth_token",
)


@pytest.mark.network
@requires_network
@has_platform_token
class TestNetworkReadOnly:
    """只读两步：99015 握手 + 1001 账号登录（**不产生任何账号数据**）。"""

    def test_handshake_then_account_login(self) -> None:
        """这两条是"整条链路是否仍然可用"的最小探针。

        之所以敢放进可选的联网测试：它们只读（1001 只返回账号态与区服列表），
        而 1003 进区会结算登录奖励、计入风控，所以被单独隔到下一层开关里。
        """
        with PlatformClient() as platform:
            user = platform.get_user_info()

        with GameClient(logger=logging.getLogger("tests.login")) as game:
            game.handshake()
            view = game.send(
                AccountClientInfoLoginPacket.for_platform_user(user.user_id),
                expect=AccountClientInfoLoginRes,
                include_defaults=True,
            )
            account = view.decode_sub_packet(AccountClientInfoLoginRes)

        assert account.errorCode == 0
        assert account.accountID > 0
        assert account.allServerList, "服务端应当下发区服列表"


@pytest.mark.network
@requires_full_login
@has_platform_token
class TestNetworkFullLogin:
    """完整登录（含 1003 进区）。会话写到**临时文件**，不动你在用的那一份。"""

    def test_full_login_fills_session_state(self, isolated_session_file: Path) -> None:
        session = offline_session()
        result = create_task("login", session).run()
        assert result.ok, result.message

        state = session.require_game_state()
        assert state.token, "1004 应当下发会话令牌"
        assert state.player_id > 0
        assert result.data["session_saved"] is True