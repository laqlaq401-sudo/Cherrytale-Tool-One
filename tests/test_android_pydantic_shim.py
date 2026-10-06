"""安卓 pydantic 垫片的行为契约（★ 2026-10-06）。

【为什么必须有这个文件】
安卓端没有真 pydantic（pydantic 2.x 依赖 Rust 写的 ``pydantic-core``，Chaquopy 的
包仓库里没有），全靠 ``tools/android_shims/pydantic/`` 这份纯 Python 垫片顶着。
**而垫片不在 ``testpaths`` 覆盖范围内** —— 桌面测试跑的是真 pydantic，
所以垫片的任何行为偏差，在 PC 上永远测不出来，只能到真机上炸。

这次翻车就是这么来的：垫片的 ``_coerce()`` 遇到 ``dict[str, 模型]``
只做 ``dict(value)``，**不把 value 收敛成声明的模型类**。
于是 ``SelectionPayload.tasks: dict[str, TaskSelectionPayload]`` 在安卓上
取出的是 plain dict，``webapi/app.py`` 的 ``entry.checked`` 直接
``AttributeError: 'dict' object has no attribute 'checked'`` → 兜底 500
「接口处理异常：POST /api/selection」。任务照跑（``/api/run`` 是另一条路），
所以用户只看到日志报错、功能没坏 —— 极难排查。

【本文件的策略：两端同一份断言】
同一组声明各造一套模型（真 pydantic 一套、垫片一套），把**同一批断言**
跑两遍。这样既钉住垫片、又顺手钉住"垫片行为必须与桌面端一致"这条总契约 ——
将来谁改了垫片的收敛规则，一眼就能看出偏到哪去了。

【为什么不把垫片塞进 ``sys.modules["pydantic"]``】
那会污染同一进程里其它测试导入的真 pydantic。这里用一个私有模块名载入，
两套实现互不干扰。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import pydantic as real_pydantic

#: 垫片源码路径（受版本控制；``android/app/src/main/python/pydantic/`` 是它的副本）
SHIM_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "tools" / "android_shims" / "pydantic" / "__init__.py"
)


def _load_shim() -> Any:
    """按私有模块名载入垫片（不覆盖真 pydantic）。"""
    name = "_android_pydantic_shim"
    spec = importlib.util.spec_from_file_location(name, SHIM_PATH)
    assert spec is not None and spec.loader is not None, f"垫片源码读不到：{SHIM_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


shim = _load_shim()


# ---------------------------------------------------------------------------
# 两套模型声明：字段、默认值、别名逐字对应，只有基类来源不同
# ---------------------------------------------------------------------------
class RealCredentials(real_pydantic.BaseModel):
    email: str = ""
    password: str = ""


class RealTaskPayload(real_pydantic.BaseModel):
    name: str = ""
    params: dict[str, Any] = real_pydantic.Field(default_factory=dict)


class RealRunPayload(real_pydantic.BaseModel):
    tasks: list[RealTaskPayload] = real_pydantic.Field(default_factory=list)
    credentials: RealCredentials | None = None


class RealTaskSelection(real_pydantic.BaseModel):
    checked: bool = real_pydantic.Field(default=False)
    params: dict[str, Any] = real_pydantic.Field(default_factory=dict)


class RealSelection(real_pydantic.BaseModel):
    tasks: dict[str, RealTaskSelection] = real_pydantic.Field(default_factory=dict)
    dry_run: bool = real_pydantic.Field(default=False)


class RealFlagMap(real_pydantic.BaseModel):
    flags: dict[str, bool] = real_pydantic.Field(default_factory=dict)


class ShimCredentials(shim.BaseModel):
    email: str = ""
    password: str = ""


class ShimTaskPayload(shim.BaseModel):
    name: str = ""
    params: dict[str, Any] = shim.Field(default_factory=dict)


class ShimRunPayload(shim.BaseModel):
    tasks: list[ShimTaskPayload] = shim.Field(default_factory=list)
    credentials: ShimCredentials | None = None


class ShimTaskSelection(shim.BaseModel):
    checked: bool = shim.Field(default=False)
    params: dict[str, Any] = shim.Field(default_factory=dict)


class ShimSelection(shim.BaseModel):
    tasks: dict[str, ShimTaskSelection] = shim.Field(default_factory=dict)
    dry_run: bool = shim.Field(default=False)


class ShimFlagMap(shim.BaseModel):
    flags: dict[str, bool] = shim.Field(default_factory=dict)


#: ``(实现, 模型组)`` —— 同一批断言在两端各跑一遍
IMPLS = [
    pytest.param(
        real_pydantic,
        {
            "selection": RealSelection,
            "task": RealTaskSelection,
            "run": RealRunPayload,
            "payload": RealTaskPayload,
            "credentials": RealCredentials,
            "flags": RealFlagMap,
        },
        id="real-pydantic",
    ),
    pytest.param(
        shim,
        {
            "selection": ShimSelection,
            "task": ShimTaskSelection,
            "run": ShimRunPayload,
            "payload": ShimTaskPayload,
            "credentials": ShimCredentials,
            "flags": ShimFlagMap,
        },
        id="android-shim",
    ),
]


def test_shim_source_exists_and_loads() -> None:
    """垫片必须能被独立载入，且导出项目实际用到的那几个名字。"""
    assert SHIM_PATH.is_file(), f"垫片源码缺失：{SHIM_PATH}"
    for name in ("BaseModel", "Field", "ValidationError", "ConfigDict"):
        assert hasattr(shim, name), f"垫片缺导出 {name}"
    # 载入时不得把真 pydantic 挤掉 —— 否则本进程里其它测试全被换实现
    assert sys.modules["pydantic"] is real_pydantic


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_of_model_yields_model_instances(impl: Any, models: dict[str, Any]) -> None:
    """★ 本次回归的主角：``dict[str, 模型]`` 的 value 必须收敛成模型实例。

    这条断言如果放宽成"能取到 checked"，就等于放过了真机上的 500 ——
    所以先断言 ``isinstance``，再断言字段值，最后**逐字复刻**
    ``webapi/app.py::save_selection`` 的写法（那里正是崩在 ``entry.checked``）。
    """
    payload = models["selection"].model_validate(
        {"tasks": {"sweep": {"checked": True, "params": {"times": 3}}}}
    )
    entry = payload.tasks["sweep"]
    assert isinstance(entry, models["task"])
    assert entry.checked is True
    assert entry.params == {"times": 3}

    saved = {
        name: {"checked": item.checked, "params": dict(item.params)}
        for name, item in payload.tasks.items()
    }
    assert saved == {"sweep": {"checked": True, "params": {"times": 3}}}


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_value_gets_defaults_when_entry_is_empty(impl: Any, models: dict[str, Any]) -> None:
    """条目是空 dict 时，模型默认值要生效（``checked=False`` / ``params={}``）。"""
    payload = models["selection"].model_validate({"tasks": {"daily_box": {}}})
    entry = payload.tasks["daily_box"]
    assert isinstance(entry, models["task"])
    assert entry.checked is False
    assert entry.params == {}


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_value_ignores_unknown_keys(impl: Any, models: dict[str, Any]) -> None:
    """条目里的多余键按 ``extra="ignore"`` 丢弃（前端与规格漂移时不攒脏数据）。"""
    payload = models["selection"].model_validate(
        {"tasks": {"sweep": {"checked": True, "junk": 1}}}
    )
    entry = payload.tasks["sweep"]
    assert entry.checked is True
    assert not hasattr(entry, "junk")


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_error_loc_points_at_the_dict_key(impl: Any, models: dict[str, Any]) -> None:
    """条目内部校验失败的报错位置必须是 ``tasks.<键>.<字段>``，两端一致。

    垫片以前会把位置报成 ``tasks.checked``（丢掉字典键、还重复字段名），
    排障时看不出到底是哪个任务写得不对。
    """
    with pytest.raises(impl.ValidationError) as excinfo:
        models["selection"].model_validate(
            {"tasks": {"sweep": {"checked": "not-a-bool"}}}
        )
    locs = [tuple(err["loc"]) for err in excinfo.value.errors()]
    assert ("tasks", "sweep", "checked") in locs


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_of_scalar_error_loc_keeps_key(impl: Any, models: dict[str, Any]) -> None:
    """``dict[str, bool]`` 这类标量 value 的报错同样要带键名。"""
    with pytest.raises(impl.ValidationError) as excinfo:
        models["flags"].model_validate({"flags": {"a": "maybe"}})
    locs = [tuple(err["loc"]) for err in excinfo.value.errors()]
    assert ("flags", "a") in locs


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_field_rejects_non_dict(impl: Any, models: dict[str, Any]) -> None:
    """字段本身不是 dict 时依旧报错（修复不能把类型检查放宽掉）。"""
    with pytest.raises(impl.ValidationError):
        models["selection"].model_validate({"tasks": "nope"})


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_dict_of_any_passes_values_through(impl: Any, models: dict[str, Any]) -> None:
    """``dict[str, Any]`` 必须原样透传（``/api/run`` 的任务参数就靠它）。"""
    run = models["run"].model_validate(
        {"tasks": [{"name": "sweep", "params": {"times": 3, "nested": {"a": [1, 2]}}}]}
    )
    assert run.tasks[0].params == {"times": 3, "nested": {"a": [1, 2]}}
    assert isinstance(run.tasks[0].params["nested"], dict)


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_list_and_optional_models_still_coerced(impl: Any, models: dict[str, Any]) -> None:
    """``list[模型]`` 与 ``模型 | None`` 是既有正确行为，修复不得碰坏。"""
    run = models["run"].model_validate(
        {
            "tasks": [{"name": "daily"}],
            "credentials": {"email": "a@b.c", "password": "x"},
        }
    )
    assert isinstance(run.tasks[0], models["payload"])
    assert run.tasks[0].name == "daily"
    assert isinstance(run.credentials, models["credentials"])
    assert run.credentials.password == "x"

    empty = models["run"].model_validate({})
    assert empty.tasks == []
    assert empty.credentials is None


@pytest.mark.parametrize(("impl", "models"), IMPLS)
def test_nested_dict_of_model(impl: Any, models: dict[str, Any]) -> None:
    """递归收敛：字典里套字典时，最内层也要是模型实例。"""
    payload = models["selection"].model_validate(
        {"tasks": {"sweep": {"params": {"deep": 1}}}}
    )
    assert isinstance(payload.tasks["sweep"], models["task"])
    assert payload.tasks["sweep"].params == {"deep": 1}
