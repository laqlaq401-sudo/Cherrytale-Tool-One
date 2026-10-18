"""内置任务的**导入清单**（"注册表是怎么被填满的"只写一遍）。

【为什么单独拆出一个模块】
任务注册是"模块导入时的副作用"：``@register_task`` 装饰器在**导入**任务模块时
才会执行。于是任何要读注册表的地方都必须先触发这些导入，否则注册表是空的。
三个地方需要它：

* ``services/runner.py`` —— 执行前解析任务类；
* ``services/task_spec.py`` —— ``describe_tasks()`` 要合并任务类的真实约束
  （需登录 / 是否花钻石 / 是否接受运行参数）；
* ``webapi/app.py`` 的 ``/api/tasks`` —— 给前端渲染复选框。

**真实踩过的坑**：一开始这份清单只存在于 ``main.py`` 里，``/api/tasks`` 用的是
"当时进程里恰好注册了什么"。命令行一切正常、单测也全绿（因为测试进程里别的
测试模块已经把它填满了），但**网页上 ``login`` 被标成"未实现"、复选框全是灰的**。
拆成独立模块、并让规格层在执行前显式加载，这类缺陷才不可能再出现。

.. note::
   新增任务文件时：**在本函数里加一行 import**，并在 ``services/task_spec.py``
   里登记 UI 规格（否则界面上会缺少中文说明，但功能不受影响）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # 仅类型检查用，避免运行期导入
    from tasks.base import BaseTask

#: 内置任务模块名白名单（与下方导入列表严格一致）
BUILTIN_MODULES: Final[frozenset[str]] = frozenset(
    {
        "tasks.arena_status",
        "tasks.battle_arena",
        "tasks.battle_experiment",
        "tasks.battle_yimo",
        "tasks.alliance_donate",
        "tasks.alliance_mining",
        "tasks.alliance_sign_in",
        "tasks.bbq",
        "tasks.buy_energy",
        "tasks.buy_arena_ticket",
        "tasks.buy_wudou_ticket",
        "tasks.buy_top_pvp_ticket",
        "tasks.daily_box",
        "tasks.daily_free_draw",
        "tasks.daily_task_auto",
        "tasks.friend_gift_claim",
        "tasks.gift_package",
        "tasks.grand_line_supply",
        "tasks.login",
        "tasks.mail",
        "tasks.main_stage",
        "tasks.market_buy",
        "tasks.material_stage",
        "tasks.probe",
        "tasks.roles",
        "tasks.sweep",
        "tasks.top_pvp",
        "tasks.top_pvp_box",
        "tasks.top_pvp_status",
        "tasks.wallet",
        "tasks.wudou_status",
        "tasks.wudou_battle",
        "tasks.yimo_box",
        "tasks.yimo_status",
    }
)


def load_builtin_tasks() -> dict[str, type[BaseTask]]:
    """导入全部内置任务模块（触发注册），返回注册表副本。

    :return: ``{任务名: 任务类}``；重复调用是**幂等**的（Python 的模块缓存保证）。

    .. note::
       ``main.load_builtin_tasks()`` 与 ``services.runner.load_builtin_tasks()``
       都只是本函数的转发，保留名字是为了不改动既有调用点与测试。
    """
    import tasks.arena_status  # noqa: F401  导入即注册（竞技场：只读次数/对手）
    import tasks.battle_arena  # noqa: F401  导入即注册（天梯竞技场：自动择弱对决）
    import tasks.battle_experiment  # noqa: F401  导入即注册（辉煌航迹试炼与终极挑战）
    import tasks.battle_yimo  # noqa: F401  导入即注册（失控炼成阵：自动挑战与发奖结算）
    import tasks.alliance_donate  # noqa: F401  导入即注册（工会金币捐献；固定 5 次）
    import tasks.alliance_mining  # noqa: F401  导入即注册（工会挖矿：个人 + 团体两个任务）
    import tasks.alliance_sign_in  # noqa: F401  导入即注册（工会签到）
    import tasks.bbq  # noqa: F401  导入即注册（宴席；含钻石消费闸门）
    import tasks.buy_energy  # noqa: F401  导入即注册（钻石购买体力；含消费闸门）
    import tasks.buy_arena_ticket  # noqa: F401  导入即注册（钻石购买竞技场挑战次数）
    import tasks.buy_wudou_ticket  # noqa: F401  导入即注册（钻石购买天命对决挑战次数）
    import tasks.buy_top_pvp_ticket  # noqa: F401  导入即注册（钻石购买荣耀之巅挑战次数）
    import tasks.daily_box  # noqa: F401  导入即注册（日常/周常活跃宝箱）
    import tasks.daily_free_draw  # noqa: F401  导入即注册（每日免费抽卡）
    import tasks.daily_task_auto  # noqa: F401  导入即注册（每日任务自动执行：补 6 个缺口动作 + 统一领奖）
    import tasks.friend_gift_claim  # noqa: F401  导入即注册（好友赠礼一键领取，10009 actionType=3）
    import tasks.gift_package  # noqa: F401  导入即注册（特惠礼包免费领取）
    import tasks.grand_line_supply  # noqa: F401  导入即注册（辉煌航迹搜集物资自动领取）
    import tasks.login  # noqa: F401  导入即注册
    import tasks.mail  # noqa: F401  导入即注册（邮件拉取与一键领取）
    import tasks.main_stage  # noqa: F401  导入即注册（主线推图与章节宝箱）
    import tasks.market_buy  # noqa: F401  导入即注册（一般市集：买满 4 种指定道具）
    import tasks.material_stage  # noqa: F401  导入即注册（素材关卡与元素试炼扫荡/首通）
    import tasks.probe  # noqa: F401  导入即注册（handshake 任务）
    import tasks.roles  # noqa: F401  导入即注册（只读角色名册：5011 + 登录快照兜底）
    import tasks.sweep  # noqa: F401  导入即注册（降临/活动关卡体力扫荡）
    import tasks.top_pvp  # noqa: F401  导入即注册（荣耀之巅：服务器结算，可自动约战）
    import tasks.top_pvp_box  # noqa: F401  导入即注册（荣耀之巅宝箱自动领取）
    # ★ 2026-10-18：荣耀之巅的**只读**状态（39001 only）。
    # 拆出来的原因：top_pvp_battle 的 battles=0 已改成"打到次数用完"，
    # 信息面板需要一个结构上不可能开打的入口（见 tasks/top_pvp_status.py）。
    import tasks.top_pvp_status  # noqa: F401  导入即注册（荣耀之巅：只读排名/积分）
    import tasks.wallet  # noqa: F401  导入即注册（只读背包：金币/钻石/体力）
    import tasks.wudou_status  # noqa: F401  导入即注册（天命对决：只读次数/名次）
    import tasks.wudou_battle  # noqa: F401  导入即注册（天命对决：自动选人与云端推演战斗）
    import tasks.yimo_box  # noqa: F401  导入即注册（失控炼成阵全服宝箱领取）
    import tasks.yimo_status  # noqa: F401  导入即注册（失控炼成阵：只读剩余次数/积分）
    from tasks.base import available_tasks

    return {
        name: task_class
        for name, task_class in available_tasks().items()
        if getattr(task_class, "__module__", "") in BUILTIN_MODULES
    }
