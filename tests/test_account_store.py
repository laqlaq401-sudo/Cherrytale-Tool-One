"""多账号令牌库（``client/account_store.py``）的单元测试 —— **全离线**。

运行方式：

    python -m pytest tests/test_account_store.py -q

【这份测试要钉住的四件事】

1. **每账号各存一份**：两个账号的令牌互不覆盖（这正是单文件 ``.auth_token`` 做不到的）；
2. **上限 5**：新增第 6 个被拒绝，但**更新已有账号不受限**；
3. **选择器**：序号 / 邮箱 / userId 三种都能选；选不中时错误必须**带着可选列表**
   （否则使用者只能猜序号，而猜错 = 用错账号进游戏）；
4. **轮换写回**：续期后新的 refreshToken 必须落盘 —— 丢了它下次续期就失败，
   而现象只是"又要输密码"，极难定位。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

import pytest

from client.account_store import AccountStore, AccountStoreError, SavedAccount
from tests.test_auth_token import LOGIN_SUCCESS, install_transport, make_response

TOKEN_A = "eyJhbGciOiJIUzI1NiJ9.ACCOUNT_A.SIG"
TOKEN_B = "eyJhbGciOiJIUzI1NiJ9.ACCOUNT_B.SIG"


def make_account(
    email: str,
    user_id: str,
    *,
    token: str = "tok",
    refresh: str = "rt",
    expires_in: float | None = 3600.0,
) -> SavedAccount:
    """造一个账号记录（``expires_in=None`` 表示读不出过期时间）。"""
    return SavedAccount(
        account=email,
        user_id=user_id,
        access_token=token,
        refresh_token=refresh,
        device_id="device-1",
        obtained_at=time.time(),
        expires_at=None if expires_in is None else time.time() + expires_in,
        last_login_at=time.time(),
    )


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> AccountStore:
    """指向临时文件的账号库（**绝不碰项目根目录那份真实账号库**）。"""
    return AccountStore(tmp_path / "accounts.json")


class TestRoundTrip:
    """存取的基本正确性。"""

    def test_upsert_then_load(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A", token=TOKEN_A))
        loaded = store.load()
        assert list(loaded) == ["ER-A"]
        assert loaded["ER-A"].access_token == TOKEN_A

    def test_two_accounts_do_not_overwrite_each_other(self, store: AccountStore) -> None:
        """多账号的核心价值：两份令牌各自保存。"""
        store.upsert(make_account("a@x.com", "ER-A", token=TOKEN_A))
        store.upsert(make_account("b@x.com", "ER-B", token=TOKEN_B))

        accounts = {item.user_id: item for item in store.list_accounts()}
        assert accounts["ER-A"].access_token == TOKEN_A
        assert accounts["ER-B"].access_token == TOKEN_B

    def test_order_is_stable_insertion_order(self, store: AccountStore) -> None:
        """序号来自插入顺序（``--account 1`` 永远指第一个登录的号）。"""
        store.upsert(make_account("a@x.com", "ER-A"))
        store.upsert(make_account("b@x.com", "ER-B"))
        assert [item.user_id for item in store.list_accounts()] == ["ER-A", "ER-B"]

    def test_update_existing_keeps_position(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A"))
        store.upsert(make_account("b@x.com", "ER-B"))
        store.upsert(make_account("a@x.com", "ER-A", token="new-token"))
        assert [item.user_id for item in store.list_accounts()] == ["ER-A", "ER-B"]
        assert store.pick("1").access_token == "new-token"

    def test_remove(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A"))
        store.upsert(make_account("b@x.com", "ER-B"))
        removed = store.remove("1")
        assert removed.user_id == "ER-A"
        assert [item.user_id for item in store.list_accounts()] == ["ER-B"]

    def test_missing_file_is_empty_store(self, store: AccountStore) -> None:
        assert store.load() == {}
        assert store.list_accounts() == []

    def test_corrupt_file_raises_with_guidance(self, store: AccountStore) -> None:
        store.path.write_text("{ 这不是 JSON", encoding="utf-8")
        with pytest.raises(AccountStoreError) as excinfo:
            store.load()
        assert "删掉" in str(excinfo.value)

    def test_unknown_keys_and_bad_values_tolerated(self, store: AccountStore) -> None:
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": {
                        "ER-X": {
                            "account": "x@x.com",
                            "access_token": "t",
                            "expires_at": "坏值",
                            "future_field": 1,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        item = store.pick("1")
        assert item.access_token == "t"
        assert item.expires_at is None  # 坏值当"没有"，而不是"已过期"
        assert item.is_expired() is False


class TestMaxAccounts:
    """上限（默认 5）—— 令牌是长期凭据，不能无限增长。"""

    def test_sixth_new_account_is_rejected(self, store: AccountStore) -> None:
        for index in range(store.max_accounts):
            store.upsert(make_account(f"a{index}@x.com", f"ER-{index}"))
        with pytest.raises(AccountStoreError) as excinfo:
            store.upsert(make_account("overflow@x.com", "ER-overflow"))
        message = str(excinfo.value)
        assert "已满" in message
        assert "main.py accounts" in message
        # 被拒绝之后不能留下半个记录
        assert len(store.list_accounts()) == store.max_accounts

    def test_updating_existing_account_at_capacity_is_allowed(
        self, store: AccountStore
    ) -> None:
        for index in range(store.max_accounts):
            store.upsert(make_account(f"a{index}@x.com", f"ER-{index}"))
        updated = store.upsert(make_account("a0@x.com", "ER-0", token="refreshed"))
        assert updated.access_token == "refreshed"
        assert len(store.list_accounts()) == store.max_accounts

    def test_limit_comes_from_config(self, tmp_path: pathlib.Path) -> None:
        """默认值来自 config（环境变量可覆盖），而不是写死在类里。"""
        import config

        assert AccountStore(tmp_path / "a.json").max_accounts == config.PLATFORM_ACCOUNTS_MAX
        assert AccountStore(tmp_path / "a.json", max_accounts=2).max_accounts == 2


class TestPick:
    """选择器：序号 / 邮箱 / userId；选不中时**必须带着可选列表**。"""

    @staticmethod
    def fill(store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A"))
        store.upsert(make_account("b@x.com", "ER-B"))

    def test_pick_by_index(self, store: AccountStore) -> None:
        self.fill(store)
        assert store.pick("1").user_id == "ER-A"
        assert store.pick("2").user_id == "ER-B"

    def test_pick_by_email_is_case_insensitive(self, store: AccountStore) -> None:
        self.fill(store)
        assert store.pick("A@X.COM").user_id == "ER-A"

    def test_pick_by_user_id(self, store: AccountStore) -> None:
        self.fill(store)
        assert store.pick("ER-B").account == "b@x.com"

    def test_single_account_needs_no_selector(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A"))
        assert store.pick().user_id == "ER-A"

    def test_multiple_accounts_without_selector_lists_options(
        self, store: AccountStore
    ) -> None:
        self.fill(store)
        with pytest.raises(AccountStoreError) as excinfo:
            store.pick()
        message = str(excinfo.value)
        assert "不止一个" in message
        assert "a@x.com" in message and "b@x.com" in message

    def test_out_of_range_index_lists_options(self, store: AccountStore) -> None:
        self.fill(store)
        with pytest.raises(AccountStoreError) as excinfo:
            store.pick("9")
        assert "超出范围" in str(excinfo.value)

    def test_unknown_selector_lists_options(self, store: AccountStore) -> None:
        self.fill(store)
        with pytest.raises(AccountStoreError) as excinfo:
            store.pick("nobody@x.com")
        assert "没有匹配" in str(excinfo.value)

    def test_empty_store_explains_how_to_add(self, store: AccountStore) -> None:
        with pytest.raises(AccountStoreError) as excinfo:
            store.pick("1")
        assert "run login" in str(excinfo.value)


class TestDescribe:
    """打印：人看得懂，且**绝不泄露令牌**。"""

    def test_describe_masks_tokens(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A", token=TOKEN_A, refresh=TOKEN_B))
        text = "\n".join(store.describe_all())
        assert TOKEN_A not in text and TOKEN_B not in text
        assert "a@x.com" in text

    def test_describe_all_is_numbered_from_one(self, store: AccountStore) -> None:
        store.upsert(make_account("a@x.com", "ER-A"))
        store.upsert(make_account("b@x.com", "ER-B"))
        lines = store.describe_all()
        assert lines[0].startswith("1) ") and lines[1].startswith("2) ")

    def test_describe_empty_store_hints(self, store: AccountStore) -> None:
        assert "账号库为空" in store.describe_all()[0]

    def test_describe_shows_last_game_and_expiry(self, store: AccountStore) -> None:
        account = make_account("a@x.com", "ER-A", expires_in=600)
        account.game = {"player_id": 123, "server_id": 167, "server_name": "宮本武藏"}
        store.upsert(account)
        text = store.describe_all()[0]
        assert "令牌剩余" in text
        assert "#167 宮本武藏" in text and "123" in text


class TestRefresh:
    """免密码续期：快过期时自动换新令牌，并把**轮换后的 refreshToken** 写回。"""

    def test_expiring_token_is_refreshed_and_persisted(
        self, store: AccountStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.upsert(
            make_account("a@x.com", "ER-A", token="old-at", refresh="old-rt", expires_in=60)
        )
        install_transport(monkeypatch, make_response(LOGIN_SUCCESS))

        refreshed, did_refresh = store.refresh_if_needed(store.pick("1"))

        assert did_refresh is True
        assert refreshed.access_token
        # ★ 关键：新令牌必须落盘（否则下次续期还是拿着旧的 refreshToken）
        persisted = store.pick("1")
        assert persisted.access_token == refreshed.access_token
        assert persisted.refresh_token == refreshed.refresh_token

    def test_fresh_token_is_left_alone(
        self, store: AccountStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """还没到期的令牌不该被续期（续期也是一次网络请求，没必要就别发）。"""
        store.upsert(make_account("a@x.com", "ER-A", token="at", expires_in=3600))
        calls = install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        account, did_refresh = store.refresh_if_needed(store.pick("1"))
        assert did_refresh is False
        assert account.access_token == "at"
        assert calls == []

    def test_no_refresh_token_means_password_needed(
        self,
        store: AccountStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store.upsert(make_account("a@x.com", "ER-A", token="at", refresh="", expires_in=-10))
        calls = install_transport(monkeypatch, make_response(LOGIN_SUCCESS))
        with caplog.at_level(logging.INFO, logger="client.accounts"):
            _account, did_refresh = store.refresh_if_needed(store.pick("1"))
        assert did_refresh is False
        assert calls == []
        assert "需要重新输入密码" in caplog.text

    def test_sessions_persistence_and_find_account(self, store: AccountStore) -> None:
        acc = make_account("multi@x.com", "ER-MULTI")
        acc.sessions["112"] = {
            "server_id": 112,
            "server_name": "S112",
            "player_id": 1111,
            "player_name": "HeroA",
            "character_level": 99,
            "token": "tok112",
        }
        acc.sessions["167"] = {
            "server_id": 167,
            "server_name": "S167",
            "player_id": 2222,
            "player_name": "HeroB",
            "character_level": 50,
            "token": "tok167",
        }
        store.upsert(acc)

        loaded = store.find_account("ER-MULTI")
        assert loaded is not None
        assert "112" in loaded.sessions
        assert "167" in loaded.sessions
        assert loaded.sessions["112"]["player_name"] == "HeroA"
        assert loaded.sessions["112"]["character_level"] == 99
        assert loaded.sessions["167"]["player_name"] == "HeroB"

        by_email = store.find_account("multi@x.com")
        assert by_email is not None and by_email.user_id == "ER-MULTI"
        assert store.find_account("nonexistent") is None