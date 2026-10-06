"""``vCode`` 生成算法的单元测试 —— **用真实抓包样本锁死**。

这个算法是逆向还原出来的。一旦被"顺手重构"改坏，症状是**所有请求都被服务端
判为包异常**（``ABNORMAL_PACKET``），而那句错误信息里看不出任何线索 ——
这次的排查代价就是证明。所以这里用两个**真实抓包样本**把整条链路钉死：

    make_v_code(1001, "", "-1", 1789905913885) == "92AEDFDDD86362AD5A1FB38C817EAC80"
    make_v_code(1003, "", "-1", 1789908064585) == "93ABA44B13800136EBED3CF097981137"

这两对值来自两次独立抓包。能同时对上，说明**拼接顺序、Base64、KEY、MD5、
大写**每一个环节都对 —— 任一处错了都不可能两个都命中。

**前置条件**：``KEY`` 已从源码中移出（公开仓库不携带该逆向常量）。
运行前需在 ``config_local.py`` 写入 ``GAME_V_CODE_KEY``，或设环境变量
``CHERRYTALE_V_CODE_KEY``。未配置时本模块全部用例 **skip**（不是 fail）——
缺 KEY 是"本机没配"，不是"算法被改坏"。

运行方式：

    python -m tests.test_vcode
    python -m pytest tests/test_vcode.py -q
"""

from __future__ import annotations

import base64
import hashlib

import pytest

import config
from crypto.vcode import V_CODE_KEY, make_v_code

#: 未配置 KEY 时跳过（而非失败）—— 见模块 docstring。
requires_key = pytest.mark.skipif(
    not V_CODE_KEY,
    reason="未配置 GAME_V_CODE_KEY（config_local.py 或 CHERRYTALE_V_CODE_KEY）；"
    "该常量不随公开仓库分发",
)


@requires_key
class TestRealSamples:
    """两个真实抓包样本（**核心断言**）。"""

    def test_login_packet_sample(self) -> None:
        """1001 请求的 vCode（来自 ``captures/raw-login-request.b64``）。"""
        assert make_v_code(1001, "", "-1", 1789905913885) == (
            "92AEDFDDD86362AD5A1FB38C817EAC80"
        )

    def test_enter_zone_sample(self) -> None:
        """1003 请求的 vCode（来自 ``captures/1003-login-zone.har``）。"""
        assert make_v_code(1003, "", "-1", 1789908064585) == (
            "93ABA44B13800136EBED3CF097981137"
        )


class TestKeyGuard:
    """KEY 缺失时必须**显式报错**，不能静默算出一个错的 vCode。"""

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import crypto.vcode as mod

        monkeypatch.setattr(mod, "V_CODE_KEY", "")
        with pytest.raises(RuntimeError, match="vCode 密钥未配置"):
            mod.make_v_code(1001, "", "-1", 1789905913885)


@requires_key
class TestShape:
    """形态与输入敏感性。"""

    def test_is_32_upper_hex(self) -> None:
        value = make_v_code(1001, "", "-1", 1789905913885)
        assert len(value) == 32
        assert value == value.upper()
        assert all(ch in "0123456789ABCDEF" for ch in value)

    def test_token_and_setting_md5_participate(self) -> None:
        """令牌 / 配置指纹变了结果必须跟着变 —— 否则说明它们没进运算。

        这两项在不同请求里确实会变（登录时为空串、进区后带令牌），
        漏掉任何一个都会让"看起来能进游戏、但发第二个包就被拒"。
        """
        base = make_v_code(1001, "", "-1", 1789905913885)
        assert make_v_code(1001, "TOKEN", "-1", 1789905913885) != base
        assert make_v_code(1001, "", "abc", 1789905913885) != base

    def test_none_token_equals_empty(self) -> None:
        """``None`` 与空串必须等价 —— 未登录时两种写法都会出现，不能算出两个结果。"""
        assert make_v_code(1001, None, "-1", 1789905913885) == make_v_code(
            1001, "", "-1", 1789905913885
        )


@requires_key
class TestAlgorithmShape:
    """按"算法应该长什么样"独立复算一遍，防止实现被改动后仍然自洽地错。"""

    @pytest.mark.parametrize(
        ("packet_id", "token", "setting_md5", "stamp"),
        [
            (1001, "", "-1", 1789905913885),
            (1003, "", "-1", 1789908064585),
            (6001, "abc", "-1", 1789910000000),
        ],
    )
    def test_matches_manual_formula(
        self, packet_id: int, token: str, setting_md5: str, stamp: int
    ) -> None:
        source = f"{packet_id}{token}{setting_md5}{stamp}"
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        expected = hashlib.md5(
            (encoded + config.GAME_V_CODE_KEY).encode()
        ).hexdigest().upper()
        assert make_v_code(packet_id, token, setting_md5, stamp) == expected
