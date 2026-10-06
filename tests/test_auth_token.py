"""平台账号密码登录（``POST /api/v2/account/login``）的单元测试 + 一次性登录脚本。

【两种用法，别混淆】

1. **当测试跑（默认，全离线）**::

       python -m pytest tests/test_auth_token.py -q

   用假传输层（``requests.Session.request`` 替身）回放两次 POST 的响应，
   断言的是"**请求形状与上游实测完全一致**"：路径、字段名、base64 密码、
   整数 ``hashCode``、空 ``Bearer``。这些错了服务端只会回含糊的错误码，
   所以必须用测试钉死。

2. **当脚本跑（会真的联网，只做"拿令牌"这一件事）**::

       python -m tests.test_auth_token                    # 交互输入账号密码（密码不回显）
       python -m tests.test_auth_token --account a@b.com   # 只问密码
       python -m tests.test_auth_token --save             # 成功后写入 .auth_token

   凭据来源优先级（脚本里写死、文档里也写死，避免"到底用了哪个"说不清）：
   ``--from-file`` → 环境变量 ``CHERRYTALE_LOGIN_ACCOUNT`` / ``CHERRYTALE_LOGIN_PASSWORD``
   → ``.platform_credentials``（若存在）→ 交互输入。

   脚本**只**做两步 POST（验证码预检 → 登录），拿到令牌打印 `eyJ…` 前缀后**立即退出**：
   不调 `/api/v2/user`、不碰游戏网关（1893）、不跑任何业务接口。

【联网用例（默认跳过）】

    CHERRYTALE_RUN_NETWORK_TESTS=1 \
    CHERRYTALE_LOGIN_ACCOUNT=... CHERRYTALE_LOGIN_PASSWORD=... \
        python -m pytest tests/test_auth_token.py -q -k network

【凭据纪律】
- 密码只从交互输入 / 环境变量 / ``.platform_credentials``（已忽略）读；
- 不写日志、不写异常信息、不进 TaskResult；
- 脚本默认**不落盘**令牌，``--save`` 才写 ``.auth_token``（也正是它让整条既有
  链路立刻可用：``main.py run login`` 需要平台令牌取 userId）。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import pathlib
import sys
import time
from typing import Any

import pytest
import requests

import config
from client.account_store import AccountStore, SavedAccount
from client.credentials import read_credentials_file
from client.exceptions import (
    ApiError,
    AuthenticationError,
    CherrytaleError,
    ProtocolError,
)
from client.platform_client import PlatformClient, encode_password, save_access_token
from models.platform import (
    PlatformToken,
    platform_token_expires_at,
    platform_token_user_id,
)

# ---------------------------------------------------------------------------
# 假数据与替身
# ---------------------------------------------------------------------------
FAKE_ACCOUNT = "someone@example.com"
FAKE_PASSWORD = "Passw0rd!"
FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.FAKEPAYLOAD.FAKESIGNATURE"
FAKE_REFRESH = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.FAKEREFRESH.FAKESIGNATURE"
FAKE_USER_ID = "ER11111111-2222-4333-8444-555555555555"
FAKE_HASH_CODE = -1222469204  # 实测值形状（可以是负数）

#: 实测的成功响应形状（字段名照抄服务端；值全部换成假值）
CAPTCHA_SUCCESS = json.dumps(
    {"status": "SUCCESS", "data": {"hashCode": FAKE_HASH_CODE}}
).encode("utf-8")

LOGIN_SUCCESS = json.dumps(
    {
        "status": "SUCCESS",
        "data": {
            "accessToken": FAKE_TOKEN,
            "refreshToken": FAKE_REFRESH,
            "userId": FAKE_USER_ID,
            "accountStatus": "NORMAL",
        },
    }
).encode("utf-8")


def business_failure(code: int, message: str) -> bytes:
    """平台业务失败信封的字节（实测失败形状：``status=FAIL`` + ``errorCode``）。"""
    return json.dumps(
        {"status": "FAIL", "message": message, "errorCode": code, "data": None},
        ensure_ascii=False,
    ).encode("utf-8")


def make_response(body: bytes, *, status_code: int = 200) -> requests.Response:
    """造一个不经过网络的 ``requests.Response``（与 tests/test_client.py 同法）。"""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.headers["Content-Type"] = "application/json"
    return response


def install_transport(
    monkeypatch: pytest.MonkeyPatch, *responses: requests.Response
) -> list[dict[str, Any]]:
    """把**所有** HTTP 请求换成"按顺序回放"的替身，并记录每次调用参数。

    :param responses: 按调用顺序给出的响应。
    :return: 每次调用的 kwargs 列表（用例靠它断言路径、body 与请求头）。

    .. note::
       这里替换的是 ``requests.Session.request`` **类方法**，而不是某个实例：
       登录流程会临时新建会话（``PlatformClient._flow_session``），
       只补实例方法会漏掉它们 —— 那样测试就会悄悄真的联网。
    """
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def fake_request(_self: requests.Session, **kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        if not queue:
            raise AssertionError("替身没有准备更多响应（测试用例漏了响应定义）")
        return queue.pop(0)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def fake_jwt(payload: dict[str, Any]) -> str:
    """造一个只有载荷有意义的假 JWT（用于验证 ``exp`` / ``user_id`` 的读取）。"""

    def segment(value: dict[str, Any]) -> str:
        raw = json.dumps(value)
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    header = {"alg": "HS256", "typ": "JWT"}
    return f"{segment(header)}.{segment(payload)}.signature"


# ---------------------------------------------------------------------------
# 离线用例：请求形状（与上游实测逐项对齐）
# ---------------------------------------------------------------------------
class TestRequestShape:
    """两次 POST 的**形状**必须与上游/前端实测一致 —— 错一位服务端只回含糊错误码。"""

    @staticmethod
    def run_login(monkeypatch: pytest.MonkeyPatch) -> tuple[PlatformToken, list[dict[str, Any]]]:
        """跑一次完整登录（全程离线），返回令牌与调用记录。"""
        calls = install_transport(
            monkeypatch, make_response(CAPTCHA_SUCCESS), make_response(LOGIN_SUCCESS)
        )
        with PlatformClient() as client:
            token = client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        return token, calls

    def test_two_requests_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _token, calls = self.run_login(monkeypatch)
        assert len(calls) == 2
        assert calls[0]["url"].endswith(config.PLATFORM_LOGIN_CAPTCHA_PATH)
        assert calls[1]["url"].endswith(config.PLATFORM_LOGIN_PATH)
        assert calls[0]["method"] == "POST" and calls[1]["method"] == "POST"

    def test_captcha_body_is_upstream_fixed_array(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _token, calls = self.run_login(monkeypatch)
        assert calls[0]["json"] == {"captcha": [0, 0, 0, 1, 0]}

    def test_login_body_fields_and_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _token, calls = self.run_login(monkeypatch)
        body = calls[1]["json"]
        assert body["account"] == FAKE_ACCOUNT
        assert body["gameId"] == config.PLATFORM_LOGIN_GAME_ID
        # hashCode 必须是**整数**（实测：上游发了 int；缺了会回 4006）
        assert body["hashCode"] == FAKE_HASH_CODE
        assert isinstance(body["hashCode"], int)

    def test_password_is_base64_not_md5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """实测门户前端是 ``btoa(明文)`` —— base64，不是 md5。"""
        import hashlib

        _token, calls = self.run_login(monkeypatch)
        encoded = calls[1]["json"]["password"]
        assert encoded == base64.b64encode(FAKE_PASSWORD.encode()).decode()
        assert encoded != hashlib.md5(FAKE_PASSWORD.encode()).hexdigest()

    def test_authorization_is_empty_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """登录/预检都带**空的** ``Bearer``（抓包实测），不能带旧令牌。

        带了旧令牌会把"失败原因"与真实原因搅在一起：服务端若按已登录态校验，
        你看到的错误就和"账号密码不对"无关了。
        """
        _token, calls = self.run_login(monkeypatch)
        for call in calls:
            assert call["headers"]["Authorization"] == "Bearer"

    def test_token_is_returned_and_applied_in_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token, _calls = self.run_login(monkeypatch)
        assert token.access_token == FAKE_TOKEN
        assert token.refresh_token == FAKE_REFRESH
        assert token.user_id == FAKE_USER_ID
        assert token.source == "credentials"

    def test_empty_credentials_send_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        with PlatformClient() as client, pytest.raises(AuthenticationError):
            client.login_by_credentials("   ", FAKE_PASSWORD)
        assert calls == []

    def test_unknown_password_encoding_is_rejected(self) -> None:
        """配置写错了要当场报错 —— 带一个错的密码去敲服务器会白计一次失败登录。"""
        with pytest.raises(ValueError):
            encode_password("x", encoding="rot13")


class TestFailureTranslation:
    """把服务端的失败翻译成**说得清原因**的异常。"""

    def test_business_error_keeps_server_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``4006 傳入參數錯誤`` 必须原样带出来（否则排查会失去唯一线索）。"""
        install_transport(
            monkeypatch,
            make_response(CAPTCHA_SUCCESS),
            make_response(business_failure(4006, "傳入參數錯誤"), status_code=400),
        )
        with PlatformClient() as client, pytest.raises(ApiError) as excinfo:
            client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        assert excinfo.value.code == 4006
        assert "傳入參數錯誤" in str(excinfo.value)

    def test_captcha_rejected_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配方失效时的现象：``4139 Captcha 驗證失敗``。"""
        install_transport(
            monkeypatch,
            make_response(business_failure(4139, "Captcha 驗證失敗"), status_code=400),
        )
        with PlatformClient() as client, pytest.raises(ApiError) as excinfo:
            client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        assert excinfo.value.code == 4139

    def test_missing_hash_code_is_protocol_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_transport(
            monkeypatch, make_response(json.dumps({"status": "SUCCESS", "data": {}}).encode())
        )
        with PlatformClient() as client, pytest.raises(ProtocolError) as excinfo:
            client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        assert "hashCode" in str(excinfo.value)

    def test_missing_access_token_is_protocol_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``status=SUCCESS`` 但没有 accessToken：说明我们对响应结构的理解有偏差。"""
        install_transport(
            monkeypatch,
            make_response(CAPTCHA_SUCCESS),
            make_response(json.dumps({"status": "SUCCESS", "data": {"userId": "x"}}).encode()),
        )
        with PlatformClient() as client, pytest.raises(ProtocolError) as excinfo:
            client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        assert "PlatformLoginData" in str(excinfo.value)


class TestTokenObject:
    """``PlatformToken``：只读 JWT 元数据 + 打码。"""

    def test_expiry_is_read_from_jwt(self) -> None:
        token = PlatformToken(access_token=fake_jwt({"exp": 1_789_977_863, "user_id": "ER-x"}))
        assert token.expires_at() == 1_789_977_863.0
        assert token.expires_in_seconds(now=1_789_977_000) == 863.0

    def test_user_id_falls_back_to_jwt_claim(self) -> None:
        value = fake_jwt({"exp": 1, "user_id": "ER-from-jwt"})
        assert platform_token_user_id(value) == "ER-from-jwt"

    def test_non_jwt_values_do_not_raise(self) -> None:
        assert platform_token_expires_at("not-a-jwt") is None
        assert platform_token_user_id("") == ""

    def test_describe_masks_the_token(self) -> None:
        token = PlatformToken(access_token=FAKE_TOKEN, user_id=FAKE_USER_ID)
        text = token.describe()
        assert FAKE_TOKEN not in text
        assert "eyJ0eX…" in text
        assert FAKE_USER_ID in text

    def test_repr_also_masks(self) -> None:
        assert FAKE_TOKEN not in repr(PlatformToken(access_token=FAKE_TOKEN))


class TestRefreshFlow:
    """续期：**不需要验证码**，请求体为空，身份靠 ``Bearer <refreshToken>``。"""

    def test_refresh_uses_bearer_and_empty_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        with PlatformClient() as client:
            token = client.refresh_access_token(FAKE_REFRESH)
        assert len(calls) == 1
        assert calls[0]["url"].endswith(config.PLATFORM_LOGIN_REFRESH_PATH)
        assert calls[0].get("json") is None, "续期请求体必须为空"
        assert calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_REFRESH}"
        assert token.source == "refresh"
        assert token.access_token == FAKE_TOKEN

    def test_missing_refresh_token_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        with PlatformClient() as client, pytest.raises(AuthenticationError):
            client.refresh_access_token("  ")
        assert calls == []


class TestCredentialHygiene:
    """凭据不得出现在日志、异常或描述里。"""

    def test_logs_contain_neither_password_nor_token(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_transport(
            monkeypatch, make_response(CAPTCHA_SUCCESS), make_response(LOGIN_SUCCESS)
        )
        with caplog.at_level(logging.DEBUG):
            with PlatformClient() as client:
                client.login_by_credentials(FAKE_ACCOUNT, FAKE_PASSWORD)
        assert FAKE_PASSWORD not in caplog.text
        assert FAKE_TOKEN not in caplog.text
        assert FAKE_REFRESH not in caplog.text

    def test_save_access_token_writes_readable_file(self, tmp_path: pathlib.Path) -> None:
        """``--save`` 写出的文件要能被既有解析链读回（``#`` 开头的行会被跳过）。"""
        target = tmp_path / ".auth_token"
        written = save_access_token(PlatformToken(access_token=FAKE_TOKEN), path=target)
        assert written == target
        text = target.read_text(encoding="utf-8")
        assert text.splitlines()[0].startswith("#")
        assert config._read_token_file(target) == FAKE_TOKEN  # noqa: SLF001 - 就是要验证它


# ---------------------------------------------------------------------------
# 独立运行形态：python -m tests.test_auth_token
# ---------------------------------------------------------------------------
def prompt_credentials(account: str | None) -> tuple[str, str]:
    """交互式收集凭据。

    密码用 ``getpass``：**不回显**，也不会留在 shell 历史里（对比命令行传参）。
    """
    import getpass

    resolved = (account or "").strip() or input("平台账号（邮箱）：").strip()
    password = getpass.getpass("平台密码（输入时不显示）：")
    return resolved, password


def build_parser() -> argparse.ArgumentParser:
    """命令行参数。默认**不落盘**任何凭据或令牌。"""
    parser = argparse.ArgumentParser(
        prog="test_auth_token.py",
        description="用账号密码换平台临时令牌（只发两次 POST，拿到就退出）",
    )
    parser.add_argument("--account", help="平台账号；省略时交互输入")
    parser.add_argument(
        "--from-file",
        type=pathlib.Path,
        default=None,
        help=f"从两行凭据文件读取（默认路径 {config.PLATFORM_CREDENTIAL_FILE}）",
    )
    parser.add_argument(
        "--game-id",
        type=int,
        default=None,
        help=f"游戏 ID（默认 {config.PLATFORM_LOGIN_GAME_ID}）",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="成功后把 access token 写入 .auth_token（默认只在内存里）",
    )
    parser.add_argument(
        "--save-to",
        type=pathlib.Path,
        default=None,
        help="把 access token 写到指定文件（隐含 --save）；用于不覆盖 .auth_token 的场景",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出账号库里已保存的账号（不发任何请求）",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="只打印结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口：**只做登录**，打印令牌前缀后立刻返回。

    :return: 成功 ``0``；登录失败 ``1``；参数/凭据问题 ``2``。
    """
    from client.exceptions import CherrytaleError
    from main import force_utf8_output

    force_utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT,
    )

    if args.list:
        store = AccountStore()
        print(f"已保存的账号（最多 {store.max_accounts} 个）：")
        try:
            for line in store.describe_all():
                print(f"  {line}")
        except CherrytaleError as exc:
            print(f"✘ {exc}")
            return 1
        return 0

    credential_file = args.from_file
    env_account = os.getenv("CHERRYTALE_LOGIN_ACCOUNT", "").strip()
    env_password = os.getenv("CHERRYTALE_LOGIN_PASSWORD", "")
    try:
        # 凭据来源优先级（写死在文档与这里，避免"到底用了哪个"说不清）：
        #   ① --from-file 明确指定的文件
        #   ② 环境变量 CHERRYTALE_LOGIN_ACCOUNT / CHERRYTALE_LOGIN_PASSWORD（自动化用）
        #   ③ 默认凭据文件 .platform_credentials（若存在）
        #   ④ 交互输入（默认路径，密码不回显）
        if credential_file is not None:
            account, password = read_credentials_file(credential_file)
            print(f"凭据来源：{credential_file}")
        elif env_account and env_password:
            account, password = env_account, env_password
            print("凭据来源：环境变量 CHERRYTALE_LOGIN_ACCOUNT / CHERRYTALE_LOGIN_PASSWORD")
        elif config.PLATFORM_CREDENTIAL_FILE.is_file():
            account, password = read_credentials_file(config.PLATFORM_CREDENTIAL_FILE)
            print(f"凭据来源：{config.PLATFORM_CREDENTIAL_FILE}")
        else:
            account, password = prompt_credentials(args.account)
    except (OSError, ValueError) as exc:
        print(f"✘ {exc}")
        return 2

    try:
        with PlatformClient() as client:
            token = client.login_by_credentials(account, password, game_id=args.game_id)
    except CherrytaleError as exc:
        print(f"\n✘ 登录失败：{exc}")
        return 1

    print(
        f"\n[✔] Token 获取成功：{token.access_token[:6]}…"
        f"（长度 {len(token.access_token)}）"
    )
    print(f"    {token.describe()}")

    # 记入账号库 → **下次就不必再输密码**（见 client/account_store.py）
    store = AccountStore()
    try:
        store.upsert(
            SavedAccount(
                account=account,
                user_id=token.user_id,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                device_id=config.DEVICE_ID,
                obtained_at=token.obtained_at,
                expires_at=token.expires_at(),
                last_login_at=time.time(),
            )
        )
    except CherrytaleError as exc:
        # 令牌已经拿到了：账号库写不进去只是"下次还得输密码"，不该报成登录失败
        print(f"    ⚠ 令牌已拿到，但没能记入账号库：{exc}")
    else:
        print(
            f"    已记入账号库（共 {len(store.list_accounts())} 个账号；"
            "`python main.py accounts` 可查看）"
        )

    if args.save_to is not None or args.save:
        # ``save_access_token(path=None)`` 会落到默认的 .auth_token
        print(f"    已写入：{save_access_token(token, path=args.save_to)}")
    else:
        print("    （未落盘；要写入 .auth_token 请加 --save，或 --save-to <路径>）")
    print("    —— 本次运行到此结束：未调用任何其它接口 ——")
    return 0


# ---------------------------------------------------------------------------
# 联网用例（默认跳过）
# ---------------------------------------------------------------------------
NETWORK_TESTS_ENABLED = os.getenv("CHERRYTALE_RUN_NETWORK_TESTS", "0") == "1"

requires_network = pytest.mark.skipif(
    not NETWORK_TESTS_ENABLED,
    reason="默认不发起真实网络请求；需要时设 CHERRYTALE_RUN_NETWORK_TESTS=1",
)
requires_login_credentials = pytest.mark.skipif(
    not (
        os.getenv("CHERRYTALE_LOGIN_ACCOUNT", "").strip()
        and os.getenv("CHERRYTALE_LOGIN_PASSWORD", "")
    ),
    reason="缺少平台账号密码；需要时设 CHERRYTALE_LOGIN_ACCOUNT / CHERRYTALE_LOGIN_PASSWORD",
)


@pytest.mark.network
@requires_network
@requires_login_credentials
class TestNetworkLogin:
    """对真实门户跑一次登录（**会产生一次登录记录**，所以默认跳过）。"""

    def test_real_login_returns_token(self) -> None:
        account = os.environ["CHERRYTALE_LOGIN_ACCOUNT"].strip()
        password = os.environ["CHERRYTALE_LOGIN_PASSWORD"]
        with PlatformClient() as client:
            token = client.login_by_credentials(account, password)

        assert token.access_token, "应当拿到 access token"
        assert len(token.access_token) > 100, "实测长度约 730 字符"
        assert token.user_id.startswith("ER"), f"userId 形如 ER…：{token.user_id!r}"
        assert token.expires_at() is not None, "JWT 里应当带 exp"
        assert token.refresh_token, "响应里应当带 refreshToken"


if __name__ == "__main__":
    raise SystemExit(main())
