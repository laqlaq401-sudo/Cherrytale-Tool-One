"""角色头像解析（``services/avatar_service.py``）与接口透出的单元测试 —— 全离线。

运行方式：

    python -m pytest tests/test_avatar_service.py -q

【这份测试要钉住的三件事】

1. **编号 → 图片的回退顺序**：运行时缓存（``PROJECT_ROOT/avatars``）优先于
   随包静态映射（``web/assets/avatars``），都没有就是 ``None`` —— 前端据此
   回退昵称首字，绝不能因为缺头像抛异常。
2. **缓存写盘的原子性**：半截下载不覆盖正式文件（``os.replace``）。
3. **接口透出**：``/api/accounts`` 的 zone 条目带 ``avatar_data_url``；
   任务页的 ``/api/player`` 走会话文件读头像，读盘失败只降级不炸接口。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from client.account_store import AccountStore, SavedAccount
from models.session_state import GameSessionState
from services import avatar_service

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fakepngdata"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """把运行时缓存指到临时目录（不污染真实工程根，测完还原）。"""
    original = avatar_service.AVATAR_CACHE_DIR
    target = tmp_path / "avatars"
    target.mkdir(parents=True, exist_ok=True)
    avatar_service.AVATAR_CACHE_DIR = target
    yield target
    avatar_service.AVATAR_CACHE_DIR = original


class TestItemDataChain:
    """主链：头像道具 itemID → ItemData.iconAssetID 去 _Icon（★ 2026-10-05 实证）。

    （★ 2026-10-06 二次勘误：上一版这里写"roleModelID/RoleInfoData 兜底链永远
    命中不了、已删除"——**结论下错了**。补 h 后 ``a001_03`` → ``a001_03h.png``
    实际命中 1394 处；旧实现不命中是因为它没补 h。现在名册角色走
    :func:`avatar_service.resolve_role_icon_code`（见 ``TestRoleIconCode``），
    与这里的"玩家自己头像"链泾渭分明。详见 services/avatar_service.py 头部。）
    """

    def test_avatar_item_maps_to_code(self) -> None:
        # ItemData 实表：200911039 = "[HCG头像]葛麗特"，iconAssetID = a001_03h_Icon
        assert avatar_service.avatar_code_from_icon(200911039) == "a001_03h"

    def test_unknown_item_reads_as_empty(self) -> None:
        assert avatar_service.avatar_code_from_icon(1) == ""
        assert avatar_service.avatar_code_from_icon(0) == ""

    def test_resolve_code_prefers_itemdata_chain(self) -> None:
        assert avatar_service.resolve_code({"icon": 200911039}) == "a001_03h"

    # ------------------------------------------------------------------
    # ★ 2026-10-06：``icon=0`` 的兜底链（"玩家头像永远出不来"的修复）
    # ------------------------------------------------------------------
    def test_resolve_code_falls_back_to_role_fields(self) -> None:
        """``icon`` 为 0 时改用 ``role_main`` / ``representative``。

        实测真实 1004（``captures/登录后自动签到（每日+活动）.saz``）里
        ``PlayerClass.icon = 0``（玩家没设过自定义头像），真实编号在
        ``IconClass.roleMainID`` / ``roleRepresentativeId`` 里。
        旧实现只读 ``icon`` ⇒ 玩家头像永远出不来。
        """
        assert (
            avatar_service.resolve_code({"icon": 0, "role_main": 200911001})
            == "e001_01h"
        ), "icon 缺省时应回退 role_main"
        assert (
            avatar_service.resolve_code({"icon": 0, "representative": 200911002})
            == "a002_02h"
        ), "role_main 也没有时应回退 representative"

    def test_icon_wins_over_role_fields(self) -> None:
        """``icon`` 是显式头像编号，优先级最高（三个键都给了时取它）。"""
        assert (
            avatar_service.resolve_code(
                {"icon": 200911039, "role_main": 200911001, "representative": 200911002}
            )
            == "a001_03h"
        )

    def test_default_avatar_falls_through_to_placeholder(self) -> None:
        """★ 真实抓包那条：``[頭像]預設`` → ``a000_01``，**刻意**不进 runtime 目录。

        ``resolve_code`` 能算出 ``a000_01``（ItemData 里确实有这条），但
        ``resolve_avatar_png`` 返回 ``None`` —— 因为那张图只在冷存目录
        （``captures/avatar_cold_storage/``），不在 runtime 静态目录。
        用户拍板（2026-10-06）：没选头像的人看我们**自制**的占位图，
        不引入游戏官方的默认剪影。所以 ``None`` 是**预期行为，不是 bug**
        —— 一旦有人把 ``a000_01.png`` 放进 runtime 目录，这条会红，
        提醒他先读 ``notes/player_avatar_diagnosis.md``。
        """
        ids = {"icon": 0, "role_main": 200913057, "representative": 200913057}
        assert avatar_service.resolve_code(ids) == "a000_01"
        assert avatar_service.resolve_avatar_png(ids) is None, (
            "默认头像必须落到占位图分支，不能悄悄进 runtime 目录"
        )

    def test_role_main_ids_are_not_item_ids(self) -> None:
        """RoleMainID（130xxxxxx）**不是** ItemData 的 key —— 传进来只会得空串。

        （名册角色走 ``resolve_role_icon_code`` —— 见 ``TestRoleIconCode``。
        两条链输入空间不同，混在一起会让两边都没法单独排错。）
        """
        assert avatar_service.resolve_code({"role_main": 130000001}) == ""
        assert (
            avatar_service.resolve_code({"icon": 999, "representative": 130000001})
            == ""
        )

    def test_bad_values_are_skipped_not_raised(self) -> None:
        """坏值（非数字）跳过、继续试下一个键，绝不抛异常。"""
        assert (
            avatar_service.resolve_code({"icon": "abc", "representative": 200911001})
            == "e001_01h"
        )
        assert avatar_service.resolve_code({"icon": None, "role_main": "x"}) == ""


class TestResolveAvatarPng:
    def test_no_ids_returns_none(self) -> None:
        assert avatar_service.resolve_avatar_png(None) is None
        assert avatar_service.resolve_avatar_png({}) is None

    def test_zero_icon_returns_none(self, cache_dir: Path) -> None:
        assert avatar_service.resolve_avatar_png({"icon": 0}) is None

    def test_runtime_cache_wins(self, cache_dir: Path) -> None:
        (cache_dir / "icon_7.png").write_bytes(PNG_BYTES)
        assert avatar_service.resolve_avatar_png({"icon": 7}) == DATA_URL

    def test_missing_everywhere_returns_none(self, cache_dir: Path) -> None:
        assert avatar_service.resolve_avatar_png({"icon": 999}) is None

    def test_bad_icon_type_returns_none(self, cache_dir: Path) -> None:
        assert avatar_service.resolve_avatar_png({"icon": "abc"}) is None

    def test_itemdata_static_mapping_hits_real_file(
        self, cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ItemData 代码 → 静态目录命中（用真实提取的 a001_03h.png）。"""
        web_dir = tmp_path / "web"
        (web_dir / "assets" / "avatars").mkdir(parents=True)
        (web_dir / "assets" / "avatars" / "a001_03h.png").write_bytes(PNG_BYTES)
        monkeypatch.setattr(avatar_service, "_web_static_dir", lambda: web_dir)
        assert (
            avatar_service.resolve_avatar_png({"icon": 200911039}) == DATA_URL
        )


class TestCacheAvatarPng:
    def test_write_then_resolve(self, cache_dir: Path) -> None:
        hit = avatar_service.cache_avatar_png(7, PNG_BYTES)
        assert hit == cache_dir / "icon_7.png"
        assert hit.read_bytes() == PNG_BYTES
        assert avatar_service.resolve_avatar_png({"icon": 7}) == DATA_URL

    def test_rejects_empty_and_zero(self, cache_dir: Path) -> None:
        assert avatar_service.cache_avatar_png(0, PNG_BYTES) is None
        assert avatar_service.cache_avatar_png(7, b"") is None
        assert list(cache_dir.glob("*")) == []

    def test_no_temp_file_left_behind(self, cache_dir: Path) -> None:
        avatar_service.cache_avatar_png(7, PNG_BYTES)
        assert list(cache_dir.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 角色头像：RoleMainID → 静态目录 code（★ 2026-10-06「我的角色」加头像）
# ---------------------------------------------------------------------------
#: 已确认带真实头像的角色（RoleMainID）→ 期望的静态文件名 code。
#:
#: 选它们当夹具的理由：**覆盖两条不同的换算路径**（一个走 RoleInfoData 主链
#: 直接命中 ``{base}h``；一个本来就没图，应回退占位）。用真名真 ID 是为了
#: 一旦配置表更新导致对应关系漂移，这里能立刻红。
#: 对应关系依据 ``notes/role_avatar_display_plan.md`` 的全量实测。
ROLE_MAIN_GOLD_KNIGHT = 130000100  # 黃金騎士 → a001_01h
ROLE_MAIN_CINDERELLA = 130000200   # 仙度瑞拉 → e001_01h
#: 炎木樹魔：自己的 icon1 在静态目录里**没有图**；同链的費妲有图但**不许兜底**。
ROLE_MAIN_FLAME_TREE = 130000300


def _avatar_dir() -> Path:
    """真工程里的静态头像目录（本文件多数用例直接读真实素材）。"""
    return Path(avatar_service.__file__).resolve().parents[1] / "web" / "assets" / "avatars"


def _real_avatars_available() -> bool:
    """静态头像目录里是否已有真图（没有时整类 Skip）。"""
    return _avatar_dir().is_dir() and any(_avatar_dir().glob("*.png"))


@pytest.fixture(autouse=True)
def _clean_role_icon_cache():
    """每个用例前后清空换算缓存 —— 用例会 monkeypatch 静态目录/配置表。"""
    avatar_service.clear_role_icon_cache()
    yield
    avatar_service.clear_role_icon_cache()


@pytest.mark.skipif(
    not _real_avatars_available(),
    reason="缺少 web/assets/avatars 静态头像（未跑 tools/extract_avatar_icons.py）",
)
class TestRoleIconCode:
    """``resolve_role_icon_code``：名册角色 → 静态文件名。

    【这个类钉死的四件事】
    1. **存在性校验**：返回的 code **必须有同名 PNG**（前端拼路径不校验，
       这里漏了就显示破图）；
    2. **不许张冠李戴**：全量扫一遍真实配置表，``icon_code`` 对应的文件名
       **不能属于另一个角色**（旧 ``roleModelID`` 路线有 28 处这种错配）；
    3. **拿不到就空**：查不到返回 ``""``，**绝不编名字**（前端回退占位图）；
    4. **占位图必须存在**：``000h.png`` 缺了整个"缺图"路径就全破图。
    """

    def test_known_roles_resolve_to_real_files(self) -> None:
        """已知角色 → 期望 code，且该文件确实存在。"""
        assert avatar_service.resolve_role_icon_code(ROLE_MAIN_GOLD_KNIGHT) == "a001_01h"
        assert avatar_service.resolve_role_icon_code(ROLE_MAIN_CINDERELLA) == "e001_01h"
        for code in ("a001_01h", "e001_01h"):
            assert (_avatar_dir() / f"{code}.png").is_file(), f"{code}.png 不在静态目录里"

    def test_role_without_art_returns_empty_not_borrowed(self) -> None:
        """★ 炎木樹魔：自己没图 → 返回空串，**不许**借用同链費妲的头像。

        这是"用错图比没图更有害"的那条判据：用户会以为角色练错了。
        """
        assert avatar_service.resolve_role_icon_code(ROLE_MAIN_FLAME_TREE) == ""

    def test_unknown_role_returns_empty(self) -> None:
        """表里没有的 ID → 空串，不抛异常。"""
        assert avatar_service.resolve_role_icon_code(0) == ""
        assert avatar_service.resolve_role_icon_code(999_999_999) == ""

    def test_placeholder_file_exists(self) -> None:
        """缺图回退的落点 ``000h.png`` 必须在（否则缺图全破图）。"""
        placeholder = avatar_service.PLACEHOLDER_ROLE_AVATAR
        assert placeholder == "000h", "占位图 code 变了要同步前端 app.js 的常量"
        assert (_avatar_dir() / f"{placeholder}.png").is_file()

    def test_every_resolved_code_has_a_file(self) -> None:
        """存在性校验的**全量**守卫：任意返回非空的 code 必有同名文件。"""
        from models.game_config import load_role_id_map

        role_ids = load_role_id_map()
        checked = 0
        for main_id in role_ids.main_ids_for_info(133003000):  # 抽一条链的代表
            code = avatar_service.resolve_role_icon_code(main_id)
            if code:
                assert (_avatar_dir() / f"{code}.png").is_file(), f"{main_id} → {code}，但文件不存在"
                checked += 1
        assert checked >= 1, "至少应有一个形态能换出头像"

    def test_no_name_mismatch_across_the_whole_table(self) -> None:
        """★ 最重要的一条：**全量扫真实表，名字冲突必须为 0**。

        判据：对每个 RoleMainID，换算出的静态文件名 → 反查它"属于"哪个角色
        （用 ``RoleInfoData`` 侧建立 文件 code → 角色名 的索引），
        该名字必须与 RoleMainID 自己的名字一致。

        旧 ``roleModelID`` 路线在这里会有 28 处冲突（如 130000100 黃金騎士
        配到仙度瑞拉的 e001_01h），所以这条是防它复活的守卫。
        """
        from models.game_config import load_role_id_map

        role_ids = load_role_id_map()

        # 文件 code → 该 code 在配置表里对应的角色名（可能多个，取集合）
        code_to_names: dict[str, set[str]] = {}
        for info_id, code in role_ids._icon_of_info.items():  # noqa: SLF001
            name = role_ids.name_of_info(info_id)
            if code and name:
                code_to_names.setdefault(code, set()).add(name)

        conflicts: list[tuple[int, str, str, set[str]]] = []
        for main_id in role_ids._icon_of_main:  # noqa: SLF001
            own_name = role_ids.name_of(main_id)
            code = avatar_service.resolve_role_icon_code(main_id)
            if not code or not own_name:
                continue
            stripped = code[:-1] if code.endswith("h") else code  # 还原成配置表裸名
            owners = code_to_names.get(stripped, set())
            if owners and own_name not in owners:
                conflicts.append((main_id, own_name, code, owners))

        assert not conflicts, (
            f"出现 {len(conflicts)} 处名字与头像不匹配（张冠李戴），"
            f"前 3 例：{conflicts[:3]}"
        )


class TestCodeVariants:
    """``_code_variants`` 的变体顺序（纯函数，不依赖真实素材）。"""

    def test_h_suffix_comes_first(self) -> None:
        """``{base}h`` 优先于 ``{base}`` —— 现役主头像都是带 h 的。"""
        assert avatar_service._code_variants("a001_03")[:2] == ("a001_03h", "a001_03")

    def test_breakthrough_forms_are_last(self) -> None:
        """超限突破形态（``_01``）放最后 —— 是命名观察，必须靠存在性兜底。"""
        variants = avatar_service._code_variants("ct116")
        assert variants == ("ct116h", "ct116", "ct116_01h", "ct116_01"), variants

    def test_non_letter_num_id_gets_no_breakthrough_form(self) -> None:
        """``a001_03`` 这种已带下划线的裸名**不**再展开突破形态。"""
        assert avatar_service._code_variants("a001_03") == ("a001_03h", "a001_03")

    def test_empty_base_returns_nothing(self) -> None:
        assert avatar_service._code_variants("") == ()


class TestPlaceholderContract:
    """占位图契约（★ 2026-10-06 用户拍板）：前后端用**同一张**自制图。

    没选头像的人（``icon=0``）与读取失败的人**一律看它** ——
    刻意**不引入**游戏官方的默认剪影 ``a000_01``。
    """

    @staticmethod
    def _web_assets() -> Path:
        return Path(avatar_service.__file__).resolve().parents[1] / "web" / "assets"

    def test_js_constant_matches_python_constant(self) -> None:
        """``common.js`` 的 ``AVATAR_PLACEHOLDER`` 与后端常量指向同一文件。"""
        expected = f"assets/avatars/{avatar_service.PLACEHOLDER_ROLE_AVATAR}.png"
        js = (self._web_assets() / "common.js").read_text(encoding="utf-8")
        assert f'const AVATAR_PLACEHOLDER = "{expected}"' in js, (
            f"common.js 的 AVATAR_PLACEHOLDER 必须等于 {expected!r}（前后端同一张图）"
        )

    def test_both_pages_use_the_shared_constant(self) -> None:
        """两处玩家头像（任务页名片 / 登录页账号卡）都回退到这个常量。"""
        for name in ("app.js", "login.js"):
            src = (self._web_assets() / name).read_text(encoding="utf-8")
            assert "AVATAR_PLACEHOLDER" in src, f"{name} 没有引用共享占位图常量"

    def test_zone_cards_no_longer_fall_back_to_first_letter(self) -> None:
        """登录页区服卡不再回退"昵称首字"（用户拍板改成占位图）。"""
        src = (self._web_assets() / "login.js").read_text(encoding="utf-8")
        assert "avatarText" not in src, (
            "login.js 里还有昵称首字兜底残留 —— 区服卡应统一回退占位图"
        )


# ---------------------------------------------------------------------------
# 接口透出：WebApp 的两个读头像点（静态方法/纯函数级，不起真实服务）
# ---------------------------------------------------------------------------
def _write_session_file(tmp_path: Path, avatar_data: str | None) -> Path:
    state = GameSessionState(
        token="FAKETOKEN00000000000000000000000000",
        player_id=1,
        avatar_data=avatar_data,
    )
    path = tmp_path / ".session.json"
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
    return path


class TestSessionAvatarDataUrl:
    def test_reads_avatar_from_session_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from webapi.app import WebApp

        path = _write_session_file(tmp_path, DATA_URL)
        monkeypatch.setenv("CHERRYTALE_SESSION_FILE", str(path))
        assert WebApp._session_avatar_data_url() == DATA_URL

    def test_missing_avatar_reads_as_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from webapi.app import WebApp

        path = _write_session_file(tmp_path, None)
        monkeypatch.setenv("CHERRYTALE_SESSION_FILE", str(path))
        assert WebApp._session_avatar_data_url() == ""

    def test_broken_session_file_reads_as_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话文件损坏时头像位降级为空串 —— 绝不让玩家信息查询失败。"""
        from webapi.app import WebApp

        path = tmp_path / ".session.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("CHERRYTALE_SESSION_FILE", str(path))
        assert WebApp._session_avatar_data_url() == ""


class TestAccountsAvatarPassthrough:
    """``/api/accounts`` 的 zone 条目透出 ``avatar_data_url``。"""

    @staticmethod
    def _accounts_payload(monkeypatch: pytest.MonkeyPatch, avatar_png: str) -> dict:
        from webapi.app import WebApp

        saved = SavedAccount(
            account="user@example.com",
            user_id="ER-1",
            sessions={
                "113": {
                    "server_id": 113,
                    "server_name": "伊莉莎白一世",
                    "player_id": 7,
                    "player_name": "真昼",
                    "character_level": 100,
                    "token": "t",
                    "avatar": {"icon": 7},
                    "avatar_png": avatar_png,
                }
            },
        )

        class FakeStore:
            def list_accounts(self) -> list[SavedAccount]:
                return [saved]

        monkeypatch.setattr(AccountStore, "list_accounts", lambda self: FakeStore().list_accounts())
        return WebApp.accounts(WebApp.__new__(WebApp))  # accounts() 不用实例状态

    def test_avatar_data_url_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = self._accounts_payload(monkeypatch, DATA_URL)
        assert payload["zone_accounts"][0]["avatar_data_url"] == DATA_URL

    def test_empty_avatar_reads_as_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = self._accounts_payload(monkeypatch, "")
        assert payload["zone_accounts"][0]["avatar_data_url"] == ""


# ---------------------------------------------------------------------------
# 直连路径（★ 2026-10-05 排查修复）：头像透传 + 直连补采
# ---------------------------------------------------------------------------
class _FakeView:
    """GameClient.send 的假返回：decode_sub_packet 按调用方需要给 1004。"""

    def __init__(self, login_res):
        self._login_res = login_res

    def decode_sub_packet(self, cls):
        return self._login_res


def _fake_login_res(icon=200911039, token="NEWTOKEN000000000000000000000000"):
    """最小 1004 响应替身（icon 用 ItemData 实表里存在的头像道具）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        errorCode=0,
        playerInitClass=SimpleNamespace(
            token=token, playerID=5, icon=icon, frameId=0, roleRepresentativeId=0
        ),
        myIconInfo=None,
    )


class _FakeGame:
    """GameClient 替身：探活与 1003→1004 都成功，envelope 记录时间戳。"""

    def __init__(self, login_res=None, fail_after_probe=False):
        from types import SimpleNamespace

        self.envelope = SimpleNamespace(time_stamp_token=777)
        self._login_res = login_res or _fake_login_res()
        self._fail_after_probe = fail_after_probe
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send(self, packet, expect=None, include_defaults=False):
        self.sent.append(packet)
        if self._fail_after_probe and len(self.sent) > 1:
            raise RuntimeError("补采包被拒")
        return _FakeView(self._login_res)


def _make_saved(avatar_png):
    return SavedAccount(
        account="user@example.com",
        user_id="ER-1",
        sessions={
            "113": {
                "server_id": 113,
                "server_name": "伊莉莎白一世",
                "player_id": 7,
                "player_name": "真昼",
                "character_level": 100,
                "account_id": 10000004,
                "token": "OLDTOKEN0000000000000000000000000",
                "time_stamp_token": 42,
                "avatar_png": avatar_png,
            }
        },
    )


class TestDirectEnterAvatar:
    """login/enter 直连分支：头像透传与补采（★ 2026-10-05 排查修复）。"""

    def _app(self, tmp_path, monkeypatch, saved):
        from webapi.app import WebApp

        monkeypatch.setattr(AccountStore, "find_account", lambda self, key: saved)
        upserts: list = []
        monkeypatch.setattr(
            AccountStore, "upsert", lambda self, account: upserts.append(account) or account
        )
        # 直连成功钩子（登录签到）不许联网
        import tasks.login_signin as signin

        monkeypatch.setattr(signin, "run_if_first_login_today", lambda state: None)
        monkeypatch.setenv("CHERRYTALE_SESSION_FILE", str(tmp_path / ".session.json"))
        return WebApp(web_dir=tmp_path / "web"), upserts

    def test_direct_enter_preserves_avatar_in_session_file(
        self, tmp_path, monkeypatch
    ) -> None:
        """账号库里有头像 → 直连后会话文件必须保留（不能被裸 state 抹成 null）。"""
        from client.game_client import GameClient
        from client.session_store import load_session_state
        from webapi.app import EnterServerPayload, WebApp

        saved = _make_saved(avatar_png=DATA_URL)
        app, _ = self._app(tmp_path, monkeypatch, saved)
        monkeypatch.setattr("client.game_client.GameClient", lambda: _FakeGame())

        result = app.login_enter(
            EnterServerPayload(direct=True, account="ER-1", server_id=113)
        )
        assert result["direct"] is True
        state = load_session_state()
        assert state.avatar_data == DATA_URL

    def test_direct_enter_captures_avatar_when_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        """账号库无头像 → 补采触发：1004 编号→静态文件→data-url，新令牌写回。"""
        from client.game_client import GameClient
        from client.session_store import load_session_state
        from webapi.app import EnterServerPayload, WebApp

        saved = _make_saved(avatar_png=None)
        app, upserts = self._app(tmp_path, monkeypatch, saved)
        monkeypatch.setattr("client.game_client.GameClient", lambda: _FakeGame())

        app.login_enter(EnterServerPayload(direct=True, account="ER-1", server_id=113))
        # 补采把新令牌写进会话文件
        state = load_session_state()
        assert state.token == "NEWTOKEN000000000000000000000000"
        # 头像走真实静态目录（web/assets/avatars/a001_03h.png 已全量提取）
        assert state.avatar_data and state.avatar_data.startswith("data:image/png;base64,")
        # 账号库条目补上了编号与图片
        assert upserts, "账号库没有被写回"
        entry = upserts[0].sessions["113"]
        assert entry["avatar"]["icon"] == 200911039
        assert entry["avatar_png"] == state.avatar_data
        assert entry["token"] == "NEWTOKEN000000000000000000000000"

    def test_capture_failure_is_silent(self, tmp_path, monkeypatch) -> None:
        """补采包被拒 → 不抛异常、直连照常成功、会话文件无头像（回退首字）。"""
        from client.game_client import GameClient
        from client.session_store import load_session_state
        from webapi.app import EnterServerPayload, WebApp

        saved = _make_saved(avatar_png=None)
        app, _ = self._app(tmp_path, monkeypatch, saved)
        monkeypatch.setattr(
            "client.game_client.GameClient", lambda: _FakeGame(fail_after_probe=True)
        )

        result = app.login_enter(
            EnterServerPayload(direct=True, account="ER-1", server_id=113)
        )
        assert result["direct"] is True
        state = load_session_state()
        assert state.avatar_data is None
        # 旧令牌原样保留（补采失败不能破坏直连会话）
        assert state.token == "OLDTOKEN0000000000000000000000000"

    def test_capture_switch_off(self, tmp_path, monkeypatch) -> None:
        """CHERRYTALE_DIRECT_AVATAR_REFRESH=0 → 不补采（send 只发生一次探活）。"""
        from client.game_client import GameClient
        from webapi.app import EnterServerPayload, WebApp

        saved = _make_saved(avatar_png=None)
        app, _ = self._app(tmp_path, monkeypatch, saved)
        game = _FakeGame()
        monkeypatch.setattr("client.game_client.GameClient", lambda: game)
        monkeypatch.setenv("CHERRYTALE_DIRECT_AVATAR_REFRESH", "0")

        app.login_enter(EnterServerPayload(direct=True, account="ER-1", server_id=113))
        assert len(game.sent) == 1  # 只有探活包
