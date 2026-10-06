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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查用，避免运行期导入
    from tasks.base import BaseTask


def load_builtin_tasks() -> dict[str, type[BaseTask]]:
    """导入全部内置任务模块（触发注册），返回注册表副本。

    :return: ``{任务名: 任务类}``；重复调用是**幂等**的（Python 的模块缓存保证）。

    .. note::
       ``main.load_builtin_tasks()`` 与 ``services.runner.load_builtin_tasks()``
       都只是本函数的转发，保留名字是为了不改动既有调用点与测试。
    """
    import tasks.arena_status  # noqa: F401  导入即注册（竞技场：只读次数/对手）
    import tasks.alliance_donate  # noqa: F401  导入即注册（工会金币捐献；固定 5 次）
    import tasks.alliance_mining  # noqa: F401  导入即注册（工会挖矿：个人 + 团体两个任务）
    import tasks.alliance_sign_in  # noqa: F401  导入即注册（工会签到）
    import tasks.bbq  # noqa: F401  导入即注册（宴席；含钻石消费闸门）
    import tasks.buy_energy  # noqa: F401  导入即注册（钻石购买体力；含消费闸门）
    import tasks.daily_box  # noqa: F401  导入即注册（日常/周常活跃宝箱）
    import tasks.daily_free_draw  # noqa: F401  导入即注册（每日免费抽卡）
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
    import tasks.wallet  # noqa: F401  导入即注册（只读背包：金币/钻石/体力）
    import tasks.wudou_status  # noqa: F401  导入即注册（天命对决：只读次数/名次）
    import tasks.yimo_box  # noqa: F401  导入即注册（失控炼成阵全服宝箱领取）
    import tasks.yimo_status  # noqa: F401  导入即注册（失控炼成阵：只读剩余次数/积分）
    from tasks.base import available_tasks

    return available_tasks()
