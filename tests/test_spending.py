"""消费许可（``client/spending.py``）的单元测试。

运行方式：

    python -m pytest tests/test_spending.py -q
    python -m tests.test_spending

【本文件要钉死的三件事（都是"钱"相关的约定）】
1. **默认关闭**：不配置任何东西时，花钻石一律被拒 —— 这是"失败即关闭"的落地；
2. 开关关闭时**任何金额都被拒**，不存在"金额很小就放行"这种中间态
   （哪怕 amount=0 的"存在花钱路径"探测也一样被拒）；
3. 只有**显式开启**后才轮到限额（单次 / 累计）生效，
   且累计值由策略对象自己记账 —— 它不可变，所以没人能在中途悄悄清零。
"""

from __future__ import annotations

import pytest

import config
from client.exceptions import CherrytaleError, SpendBlockedError
from client.spending import DENY_ALL, SpendKind, SpendPolicy


class TestDefaultClosed:
    """默认状态就必须是"拒绝"。"""

    def test_default_policy_denies_diamond(self) -> None:
        with pytest.raises(SpendBlockedError):
            SpendPolicy().check(kind=SpendKind.DIAMOND, amount=1, reason="测试")

    def test_default_flag_is_false(self) -> None:
        """``config`` 的全局开关默认必须是关闭的（除非用户显式配置）。"""
        assert config.ALLOW_DIAMOND_SPEND is False

    def test_zero_amount_also_denied(self) -> None:
        """amount=0 表示"这里存在花钱路径"，同样必须被拒。"""
        with pytest.raises(SpendBlockedError):
            DENY_ALL.check(kind=SpendKind.DIAMOND, amount=0, reason="存在花钱路径")

    def test_item_spend_denied_by_empty_whitelist(self) -> None:
        """道具白名单为空 = 一个都不允许（而不是"不限"）。"""
        with pytest.raises(SpendBlockedError):
            SpendPolicy(allow_diamond=True).check(
                kind=SpendKind.ITEM, amount=1, reason="道具消费"
            )

    def test_error_message_tells_how_to_enable(self) -> None:
        """被拦下时，报错信息必须告诉用户"怎么开"。"""
        with pytest.raises(SpendBlockedError) as info:
            SpendPolicy().check(kind=SpendKind.DIAMOND, amount=7, reason="宴席")
        text = str(info.value)
        assert config.ALLOW_DIAMOND_SPEND_ENV_NAME in text
        assert "--allow-diamond" in text

    def test_is_cherrytale_error(self) -> None:
        """必须是自家异常基类的子类，这样 BaseTask.run 才会转成失败结果而非崩溃。"""
        assert issubclass(SpendBlockedError, CherrytaleError)


class TestFromConfig:
    """``from_config()`` 必须忠实跟随 ``config.ALLOW_DIAMOND_SPEND``。"""

    def test_follows_config_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "ALLOW_DIAMOND_SPEND", True)
        assert SpendPolicy.from_config().allow_diamond is True

        monkeypatch.setattr(config, "ALLOW_DIAMOND_SPEND", False)
        assert SpendPolicy.from_config().allow_diamond is False


class TestLimits:
    """限额只在开关开启后才有意义。"""

    def test_per_call_limit(self) -> None:
        policy = SpendPolicy(allow_diamond=True, max_diamond_per_call=7)
        assert policy.check(kind=SpendKind.DIAMOND, amount=7, reason="第 2 档").spent_so_far == 7
        with pytest.raises(SpendBlockedError):
            policy.check(kind=SpendKind.DIAMOND, amount=8, reason="第 3 档")

    def test_per_run_limit_accumulates_across_checks(self) -> None:
        """累计上限必须跨多笔消费生效（这正是把它放在策略对象上的原因）。"""
        policy = SpendPolicy(allow_diamond=True, max_diamond_per_run=10)
        after_first = policy.check(kind=SpendKind.DIAMOND, amount=7, reason="第 1 笔")
        after_second = after_first.check(kind=SpendKind.DIAMOND, amount=3, reason="第 2 笔")
        assert after_second.spent_so_far == 10

        with pytest.raises(SpendBlockedError):
            after_second.check(kind=SpendKind.DIAMOND, amount=1, reason="第 3 笔")

    def test_charge_returns_new_object(self) -> None:
        """不可变：charge 生成新对象，原对象的累计值不变。"""
        base = SpendPolicy(allow_diamond=True)
        after = base.charge(kind=SpendKind.DIAMOND, amount=5)
        assert base.spent_so_far == 0
        assert after.spent_so_far == 5
        assert after is not base

    def test_limits_do_not_bypass_closed_switch(self) -> None:
        """开关关闭时，限额不是"另一种开启方式"。"""
        policy = SpendPolicy(allow_diamond=False, max_diamond_per_call=999)
        with pytest.raises(SpendBlockedError):
            policy.check(kind=SpendKind.DIAMOND, amount=1, reason="测试")


class TestDescribe:
    """``describe()`` 是给人看的，必须能一眼看出开关状态。"""

    def test_closed_text(self) -> None:
        assert "禁止" in SpendPolicy().describe()

    def test_open_text_contains_limits(self) -> None:
        text = SpendPolicy(allow_diamond=True, max_diamond_per_run=20).describe()
        assert "允许" in text
        assert "20" in text


class TestRefund:
    def test_refund_reduces_spent(self) -> None:
        """★ 2026-10-04：服务端拒绝时冲回预扣（宴席 -6 事故的记账修正）。"""
        policy = SpendPolicy(allow_diamond=True).charge(
            kind=SpendKind.DIAMOND, amount=31
        )
        refunded = policy.refund(31)
        assert refunded.spent_so_far == 0
        # 原对象不变（不可变策略，流水清晰）
        assert policy.spent_so_far == 31

    def test_refund_floors_at_zero(self) -> None:
        policy = SpendPolicy(allow_diamond=True)
        assert policy.refund(31).spent_so_far == 0


if __name__ == "__main__":  # 支持 `python -m tests.test_spending`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
