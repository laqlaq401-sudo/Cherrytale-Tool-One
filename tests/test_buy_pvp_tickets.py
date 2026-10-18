"""竞技场、天命对决与荣耀之巅门票购买任务的单元测试。

验证内容：
1. 阶梯费用计算器（calculate_pvp_cost）；
2. VIP 购买上限映射（get_vip_max_buy_times）；
3. 消费闸门与用途拦截（SpendPurpose 校验）；
4. 超量防护（已达上限安全退出，超量自动截断）；
5. 演练模式（dry_run 零发包）。
"""

from __future__ import annotations

import pytest

from client.exceptions import SpendBlockedError
from client.spending import SpendPolicy, SpendPurpose
from models.buy_pvp import (
    PVP_STEP_COSTS,
    calculate_pvp_cost,
    get_vip_max_buy_times,
)
from tasks.buy_arena_ticket import BuyArenaTicketTask
from tasks.buy_wudou_ticket import BuyWuDouTicketTask
from tasks.buy_top_pvp_ticket import BuyTopPvpTicketTask


class TestPvpStepCostCalculator:
    """测试阶梯计费逻辑。"""

    def test_zero_times(self) -> None:
        assert calculate_pvp_cost(0, 0) == 0
        assert calculate_pvp_cost(5, 0) == 0

    def test_first_few_purchases(self) -> None:
        # 从 0 开始买 1 次：10
        assert calculate_pvp_cost(0, 1) == 10
        # 从 0 开始买 2 次：10 + 10 = 20
        assert calculate_pvp_cost(0, 2) == 20
        # 从 0 开始买 3 次：10 + 10 + 20 = 40
        assert calculate_pvp_cost(0, 3) == 40
        # 从 0 开始买 5 次：10 + 10 + 20 + 30 + 40 = 110
        assert calculate_pvp_cost(0, 5) == 110

    def test_cross_tier_purchase(self) -> None:
        # 已购 2 次，再买 2 次（第 3、4 次：20 + 30 = 50）
        assert calculate_pvp_cost(2, 2) == 50
        # 已购 5 次，再买 2 次（第 6、7 次：55 + 70 = 125）
        assert calculate_pvp_cost(5, 2) == 125


class TestVipMaxBuyLimits:
    """测试各系统 VIP 购买上限映射。"""

    def test_arena_limits(self) -> None:
        assert get_vip_max_buy_times(0, "arena") == 10
        assert get_vip_max_buy_times(5, "arena") == 15
        assert get_vip_max_buy_times(8, "arena") == 20
        assert get_vip_max_buy_times(11, "arena") == 25
        assert get_vip_max_buy_times(14, "arena") == 30
        assert get_vip_max_buy_times(15, "arena") == 30

    def test_wudou_limits(self) -> None:
        assert get_vip_max_buy_times(0, "wudou") == 10
        assert get_vip_max_buy_times(5, "wudou") == 15
        assert get_vip_max_buy_times(8, "wudou") == 20
        assert get_vip_max_buy_times(11, "wudou") == 25
        assert get_vip_max_buy_times(15, "wudou") == 25

    def test_top_pvp_limits(self) -> None:
        assert get_vip_max_buy_times(0, "top_pvp") == 5
        assert get_vip_max_buy_times(5, "top_pvp") == 10
        assert get_vip_max_buy_times(7, "top_pvp") == 15
        assert get_vip_max_buy_times(9, "top_pvp") == 20
        assert get_vip_max_buy_times(12, "top_pvp") == 25
        assert get_vip_max_buy_times(15, "top_pvp") == 30


class TestTaskDeclarations:
    """测试任务类级约束声明。"""

    @pytest.mark.parametrize(
        "task_cls, purpose",
        [
            (BuyArenaTicketTask, SpendPurpose.BUY_ARENA_TICKET),
            (BuyWuDouTicketTask, SpendPurpose.BUY_WUDOU_TICKET),
            (BuyTopPvpTicketTask, SpendPurpose.BUY_TOP_PVP_TICKET),
        ],
    )
    def test_declarations(self, task_cls: type, purpose: SpendPurpose) -> None:
        assert task_cls.requires_auth is True
        assert task_cls.spends_diamond is False
        assert task_cls.diamond_purposes == (purpose,)


class FakeSession:
    """测试用假会话。"""
    game_state = None


class TestDiamondSpendGate:
    """测试钻石总闸与消费拦截。"""

    def test_arena_buy_blocked_when_diamond_disallowed(self) -> None:
        policy = SpendPolicy(allow_diamond=False)
        task = BuyArenaTicketTask(
            FakeSession(),
            spend_policy=policy,
            times=1,
            dry_run=False,
        )
        # 未开总闸时，调用 request_diamond_spend 必被拦截
        with pytest.raises(SpendBlockedError) as exc_info:
            task.request_diamond_spend(10, reason="test", purpose=SpendPurpose.BUY_ARENA_TICKET)
        assert "钻石使用开关处于【关闭】状态" in str(exc_info.value)

    def test_arena_buy_blocked_when_purpose_not_allowed(self) -> None:
        policy = SpendPolicy(
            allow_diamond=True,
            allowed_purposes=frozenset({SpendPurpose.BUY_ENERGY}),  # 仅允许体力，未允许竞技场
        )
        task = BuyArenaTicketTask(
            FakeSession(),
            spend_policy=policy,
            times=1,
            dry_run=False,
        )
        with pytest.raises(SpendBlockedError) as exc_info:
            task.request_diamond_spend(10, reason="test", purpose=SpendPurpose.BUY_ARENA_TICKET)
        assert "未被授权" in str(exc_info.value)
