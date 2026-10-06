"""纯 Python 的 pydantic 最小垫片（只随安卓包分发，桌面环境不受影响）。

【为什么需要这个文件】
pydantic 2.x 强依赖 Rust 编写的 ``pydantic-core``，而 Chaquopy（安卓上的
Python 运行时）的预编译包仓库里**没有** pydantic-core，pip 解析必然失败 ——
这是整个安卓封包唯一绕不过去的硬阻断（fastapi 同理装不上，安卓端改用
``webapi/android_app.py`` 标准库服务器解决）。

【为什么敢用垫片】
本项目对 pydantic 的使用面极窄且稳定（2026-10-02 全量 grep 确认）：

- 导入：``BaseModel`` / ``Field`` / ``ConfigDict`` / ``ValidationError`` 四个名字；
- ``Field`` 只用 ``default`` / ``default_factory`` / ``alias`` / ``description``；
- 零 validator、零 Annotated、零 RootModel；
- 方法只用了 ``model_validate`` / ``model_dump(exclude_none, by_alias)`` /
  ``model_copy(update)`` / ``model_fields``（类访问）。

桌面端与安卓端跑的是**同一批**模型代码；桌面端（真 pydantic）把行为
定住，安卓端只需在同一份数据上表现一致。因此本垫片按"宽松但不静默丢错"
的原则实现：字段类型做温和收敛，缺必填项如实抛 ``ValidationError``，
多余字段按 ``extra="ignore"`` 丢弃（真 pydantic v2 的默认行为，也是本项目
协议模型的显式约定）。

【同步须知】
本文件只存在于安卓工程里，``tools/sync_android_assets.py`` 同步代码时
**不得覆盖或删除** ``android/app/src/main/python/pydantic/``。
"""

from __future__ import annotations

import copy as _copy
import sys as _sys
import types as _types
import typing as _t

__all__ = [
    "BaseModel",
    "ConfigDict",
    "Field",
    "FieldInfo",
    "ValidationError",
    "__version__",
]

#: 版本号保持与桌面 requirements.txt 同主版本，便于排查时对照行为。
__version__ = "2.13.5-shim"


class ConfigDict(dict):
    """模型的配置项（真 pydantic 里是 TypedDict，这里 dict 足够）。"""


class _UndefinedType:
    """「未提供默认值」的哨兵——区别于 ``default=None``（显式允许空）。"""

    def __repr__(self) -> str:
        return "PydanticUndefined"


PydanticUndefined = _UndefinedType()


class FieldInfo:
    """单个字段的声明信息（由 :func:`Field` 创建）。"""

    def __init__(
        self,
        default: object = PydanticUndefined,
        default_factory: object = PydanticUndefined,
        alias: str | None = None,
        description: str | None = None,
        **extra: object,
    ) -> None:
        self.default = default
        self.default_factory = default_factory
        self.alias = alias
        self.description = description
        self.extra = extra
        # 注解由 BaseModel._resolve_fields 在解析阶段写回（真 pydantic 的公开同名属性）。
        self.annotation: object = _t.Any

    def get_default(self) -> object:
        """按真实 pydantic 语义取默认值：每次调用生成新对象。

        真实 pydantic 对可变默认值（``= []`` / ``= {}``）做深拷贝 —— 每个实例
        独享一份；直接共享会让两个模型的字段指向同一个列表（项目里
        ``models/envelope.py`` 的 ``meta``、``models/config_table.py`` 的
        ``rows`` 都是这种写法）。
        """
        if self.default_factory is not PydanticUndefined:
            return self.default_factory()  # type: ignore[operator]
        if self.default is not PydanticUndefined:
            value = self.default
            if isinstance(value, (list, dict, set, bytearray)):
                return _copy.deepcopy(value)
            return value
        return PydanticUndefined

    @property
    def is_required(self) -> bool:
        return (
            self.default is PydanticUndefined
            and self.default_factory is PydanticUndefined
        )


def Field(
    default: object = PydanticUndefined,
    *,
    default_factory: object = PydanticUndefined,
    alias: str | None = None,
    description: str | None = None,
    **extra: object,
) -> FieldInfo:
    """声明字段（本项目只用到 default / default_factory / alias / description）。"""
    return FieldInfo(
        default=default,
        default_factory=default_factory,
        alias=alias,
        description=description,
        **extra,
    )


class ValidationError(ValueError):
    """字段校验失败。

    ``errors()`` 返回 ``[{"loc": (字段名, ...), "msg": str, "type": str}, ...]``；
    ``str(exc)`` 是面向人的多行文本（与真 pydantic 的输出风格一致，便于
    ``models/base.py::from_json`` 直接把它拼进错误说明里）。
    """

    def __init__(self, title: str, errors: list[dict[str, object]]) -> None:
        self.title = title
        self._errors = errors
        count = len(errors)
        super().__init__(
            f"{count} validation error{'s' if count != 1 else ''} for {title}"
        )

    def errors(self) -> list[dict[str, object]]:
        return list(self._errors)

    def __str__(self) -> str:
        lines = [str(self.args[0])]
        for err in self._errors:
            loc = ".".join(str(part) for part in err.get("loc", ()))
            target = loc if loc else "<root>"
            lines.append(f"{target}: {err.get('msg', 'invalid')}")
        return "\n".join(lines)


def _type_error(name: str, msg: str, kind: str) -> ValidationError:
    """构造"字段值本身不合法"的错误。

    ⚠️ ``loc`` 留空（``()``）: 路径由调用方缀 —— ``_validate_dict`` 缀字段名、
    dict 分支缀字典键、list 分支缀下标。以前这里自己就带了一次 ``name``，
    于是同一个字段名被缀两遍（真 pydantic 报 ``tasks.sweep.checked``，
    垫片报成 ``tasks.sweep.checked.checked``），排障时看不出该去改哪个字段。
    ``name`` 只在 ``title`` 里留个线索；对外抛出的 title 会被 ``_validate_dict``
    换成模型类名，不影响 ``str(exc)`` 的最终形态。
    """
    return ValidationError(
        f"<{name}>", [{"loc": (), "msg": msg, "type": kind}]
    )


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """把 ``X | None`` / ``Optional[X]`` 拆成 ``(X, 可否为 None)``。"""
    origin = _t.get_origin(annotation)
    is_union = origin is _t.Union or (
        getattr(_types, "UnionType", None) is not None
        and origin is _types.UnionType
    )
    if not is_union:
        return annotation, False
    raw_args = _t.get_args(annotation)
    args = [a for a in raw_args if a is not type(None)]
    if len(args) == 1:
        return args[0], len(args) != len(raw_args)
    return annotation, True  # 复杂 Union：整体透传，不再深拆


def _is_model(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _coerce(name: str, annotation: object, value: object) -> object:
    """把外部数据收敛成注解要求的形态（宽松模式，详见模块文档）。"""
    if value is None:
        return None

    inner, _allow_none = _unwrap_optional(annotation)
    if inner is not annotation:
        return _coerce(name, inner, value)

    if inner is _t.Any or inner is object:
        return value

    if _is_model(inner):
        if isinstance(value, inner):
            return value
        if isinstance(value, dict):
            return inner.model_validate(value)
        raise _type_error(name, f"需要 {inner.__name__} 对象或 dict", "model_type")

    origin = _t.get_origin(inner)
    if origin in (list, tuple):
        args = _t.get_args(inner)
        item_type = args[0] if args else _t.Any
        if isinstance(value, (list, tuple)):
            return [_coerce(name, item_type, item) for item in value]
        raise _type_error(name, "需要列表", "list_type")
    if origin is dict:
        if not isinstance(value, dict):
            raise _type_error(name, "需要字典", "dict_type")
        args = _t.get_args(inner)
        if len(args) != 2:
            return dict(value)  # 裸 ``dict``（无参数）：没有可收敛的类型，浅拷即可
        _key_type, value_type = args
        # 【★ 2026-10-06 修复】这里必须**按声明的 value 类型递归收敛**，
        # 不能像以前那样一句 ``dict(value)`` 了事。
        # 漏掉收敛的后果实测过：``SelectionPayload.tasks: dict[str, TaskSelectionPayload]``
        # 在安卓上取出来是 plain dict，``webapi/app.py`` 里 ``entry.checked`` 直接
        # ``AttributeError: 'dict' object has no attribute 'checked'`` → 兜底 500
        # 「接口处理异常：POST /api/selection」，而 PC（真 pydantic）永远复现不了。
        # key 不转换：JSON 对象键天然是 str，项目里也没有非 str 键的模型字段，
        # 强行"按注解转键"只会引入与真 pydantic 的分歧。
        out: dict[object, object] = {}
        for key, item in value.items():
            try:
                out[key] = _coerce(name, value_type, item)
            except ValidationError as exc:
                # 把内层报错的位置缀上字典键：``_coerce`` 抛出的 loc 是**相对它那次
                # 调用**的（字段级为空、嵌套时是自己的子路径），这里加键、
                # ``_validate_dict`` 再加字段名 → 最终 ``tasks.sweep.checked``。
                errors: list[dict[str, object]] = []
                for err in exc.errors():
                    inner = tuple(err.get("loc", ()))  # type: ignore[arg-type]
                    errors.append({**err, "loc": (key, *inner)})
                raise ValidationError("<model>", errors) from None
        return out

    if inner is bool:
        # 真实 pydantic 宽松模式接受 0/1 与 true/false 词集（前端 JS 常发 0/1）
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "on", "y", "t", "1"):
                return True
            if lowered in ("false", "no", "off", "n", "f", "0"):
                return False
        raise _type_error(name, "需要布尔值", "bool_type")
    if inner is int:
        if isinstance(value, bool):
            raise _type_error(name, "整数不能是布尔值", "int_type")
        if isinstance(value, float):
            # 真实 pydantic 只接受"整数值"的浮点数（3.0 可以，3.5 拒绝），
            # 静默截断会把协议数值改错
            if value.is_integer():
                return int(value)
            raise _type_error(name, f"{value!r} 不是整数", "int_type")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise _type_error(name, f"无法把 {value!r} 转成整数", "int_type") from None
    if inner is float:
        try:
            return float(value)  # int 也接受（与真 pydantic 宽松模式一致）
        except (TypeError, ValueError):
            raise _type_error(name, f"无法把 {value!r} 转成浮点数", "float_type") from None
    if inner is str:
        # 与真 pydantic 宽松模式一致：只收 str 与 bytes。
        # 【不能 str(value) 兜底】协议模型的价值就在于"类型不对立刻报错"，
        # 静默把 dict/数字转成字符串会把错误藏进报文（tests/test_models.py
        # 的 test_wrong_type_raises 守的就是这条契约）。
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        raise _type_error(name, f"需要字符串，收到 {type(value).__name__}", "string_type")
    if inner is bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        raise _type_error(name, "需要 bytes 或 str", "bytes_type")

    return value  # 未知注解（含自定义类）：原样透传


def _dump_value(value: object, *, by_alias: bool, exclude_none: bool) -> object:
    """递归序列化：模型 → dict，列表/字典 → 逐项处理，其余原样。"""
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=by_alias, exclude_none=exclude_none)
    if isinstance(value, (list, tuple)):
        return [
            _dump_value(item, by_alias=by_alias, exclude_none=exclude_none)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _dump_value(item, by_alias=by_alias, exclude_none=exclude_none)
            for key, item in value.items()
        }
    return value


class _ModelMeta(type):
    """收集字段声明；注解解析**懒执行**（真 pydantic 同样容忍前向引用）。

    前向引用有个真 pydantic 也面对过的坑：类定义在**函数内部**时（测试里
    常见），注解里的名字既不在模块全局、也不在类命名空间里，懒解析时
    ``get_type_hints`` 会抛 ``NameError``。真 pydantic 的解法是类创建时
    检查调用帧；这里照做 —— ``__new__`` 里把调用者的局部变量快照下来
    （模块级类不快照：那个帧的 locals 就是模块 globals，白存一份）。
    """

    @property
    def model_fields(cls) -> dict[str, FieldInfo]:
        return cls._resolve_fields()

    @property
    def __pydantic_complete__(cls) -> bool:
        return True

    def __new__(mcls, name: str, bases: tuple, namespace: dict, **kwargs: object):
        frame = _sys._getframe(1)
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)
        module_ns = _sys.modules.get(frame.f_globals.get("__name__", ""))  # type: ignore[arg-type]
        if module_ns is None or frame.f_locals is not vars(module_ns):
            cls._frame_locals = dict(frame.f_locals)
        return cls

    def __call__(cls, **data: object) -> BaseModel:
        values = cls._validate_dict(data)
        instance = cls.__new__(cls)
        object.__setattr__(instance, "__dict__", values)
        return instance


class BaseModel(metaclass=_ModelMeta):
    """所有协议模型 / 请求体的基类（行为对齐真 pydantic v2 的默认值）。

    - ``extra`` 默认 ``"ignore"``（真 pydantic v2 的默认就是 ignore）；
    - ``populate_by_name`` 始终允许（字段名与别名都能赋值，是本项目的约定）。
    """

    model_config: ConfigDict = ConfigDict()
    __pydantic_complete__: bool = True

    @classmethod
    def model_rebuild(
        cls,
        *,
        force: bool = False,
        raise_errors: bool = True,
        _parent_namespace_depth: int = 2,
        _types_namespace: dict[str, object] | None = None,
    ) -> bool | None:
        """重新解析字段类型（兼容 pydantic v2 的 model_rebuild）。"""
        if hasattr(cls, "_fields_cache"):
            try:
                delattr(cls, "_fields_cache")
            except AttributeError:
                pass
        cls._resolve_fields()
        return True

    # ------------------------------------------------------------------
    # 字段解析（懒执行 + 按类缓存）
    # ------------------------------------------------------------------
    @classmethod
    def _resolve_fields(cls) -> dict[str, FieldInfo]:
        cached = cls.__dict__.get("_fields_cache")
        if cached is not None:
            return cached

        # 基类先解析（递归），再按"基类在前、子类覆盖在后"合并 —— 与真 pydantic 一致。
        fields: dict[str, FieldInfo] = {}
        for klass in reversed(cls.__mro__):
            if klass is cls or not isinstance(klass, _ModelMeta):
                continue
            fields.update(klass._resolve_fields())

        hints = cls._resolve_hints()
        for name, annotation in hints.items():
            if name.startswith("_") or name in ("model_config", "model_fields"):
                continue
            if _t.get_origin(annotation) is _t.ClassVar:
                continue
            declared = cls.__dict__.get(name)
            if callable(declared) and not isinstance(declared, FieldInfo):
                continue  # 方法不是字段
            if isinstance(declared, FieldInfo):
                declared.annotation = annotation
                fields[name] = declared
            elif name in cls.__dict__:
                info = FieldInfo(default=declared)
                info.annotation = annotation
                fields[name] = info
            elif name in fields:
                # 【继承】get_type_hints 会把基类的注解一并带进来；子类没有重新
                # 声明时必须保留基类字段（含默认值/别名），只刷新注解。
                # 2026-10-02 真机踩坑：EnterServerPayload(LoginPayload) 的
                # account 由此被重置成必填，选区后进区直接 400。
                fields[name].annotation = annotation
            else:
                info = FieldInfo()
                info.annotation = annotation
                fields[name] = info

        cls._fields_cache = fields
        return fields

    @classmethod
    def _resolve_hints(cls) -> dict[str, object]:
        """解析全部字段注解；个别无法解析的名字降级为 ``Any``（透传），不炸整类。"""
        localns = cls.__dict__.get("_frame_locals") or {}
        try:
            return dict(_t.get_type_hints(cls, localns=localns))  # type: ignore[arg-type]
        except NameError:
            merged: dict[str, object] = {}
            for klass in cls.__mro__:
                merged.update(vars(klass).get("__annotations__", {}) or {})
            module_ns: dict[str, object] = {}
            module = _sys.modules.get(cls.__module__)
            if module is not None:
                module_ns = vars(module)
            hints: dict[str, object] = {}
            for name, ann in merged.items():
                if isinstance(ann, str):
                    try:
                        hints[name] = eval(ann, dict(module_ns), localns)  # noqa: S307
                    except Exception:  # noqa: BLE001 — 解析不了的注解按 Any 透传
                        hints[name] = _t.Any
                else:
                    hints[name] = ann
            return hints

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @classmethod
    def _config_value(cls, key: str) -> object:
        for klass in cls.__mro__:
            config = klass.__dict__.get("model_config")
            if isinstance(config, dict) and key in config:
                return config[key]
        return None

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @classmethod
    def _validate_dict(cls, data: object) -> dict[str, object]:
        if not isinstance(data, dict):
            raise ValidationError(
                cls.__name__,
                [{"loc": (), "msg": "需要 JSON 对象（dict）", "type": "dict_type"}],
            )
        fields = cls._resolve_fields()
        by_alias = {fi.alias: name for name, fi in fields.items() if fi.alias}
        values: dict[str, object] = {}
        errors: list[dict[str, object]] = []

        for key, raw in data.items():
            name = key if key in fields else by_alias.get(key)
            if name is None:
                if cls._config_value("extra") == "forbid":
                    errors.append(
                        {"loc": (key,), "msg": "多余字段", "type": "extra_forbidden"}
                    )
                continue  # extra="ignore"（默认）：直接丢弃
            fi = fields[name]
            try:
                values[name] = _coerce(name, fi.annotation, raw)
            except ValidationError as exc:
                for err in exc.errors():
                    errors.append(
                        {
                            "loc": (name, *err["loc"]),  # type: ignore[operator]
                            "msg": err["msg"],
                            "type": err["type"],
                        }
                    )

        for name, fi in fields.items():
            if name in values:
                continue
            default = fi.get_default()
            if default is not PydanticUndefined:
                values[name] = default
            else:
                errors.append(
                    {
                        "loc": (name,),
                        "msg": "Field required（缺少必填字段）",
                        "type": "missing",
                    }
                )

        if errors:
            raise ValidationError(cls.__name__, errors)
        return values

    @classmethod
    def model_validate(cls, data: object) -> BaseModel:
        """从 dict 构造模型（数据层入口，行为对齐真 pydantic）。"""
        return cls(**data)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def model_dump(
        self,
        *,
        by_alias: bool = False,
        exclude_none: bool = False,
        **_kwargs: object,
    ) -> dict[str, object]:
        fields = type(self)._resolve_fields()
        out: dict[str, object] = {}
        for name, fi in fields.items():
            value = self.__dict__.get(name, PydanticUndefined)
            if value is PydanticUndefined:
                value = fi.get_default()
            if exclude_none and value is None:
                continue
            key = fi.alias if (by_alias and fi.alias) else name
            out[key] = _dump_value(value, by_alias=by_alias, exclude_none=exclude_none)
        return out

    def model_copy(self, *, update: dict[str, object] | None = None) -> BaseModel:
        clone = self.__class__.__new__(self.__class__)
        object.__setattr__(clone, "__dict__", dict(self.__dict__, **(update or {})))
        return clone

    def __eq__(self, other: object) -> bool:
        # 与真 pydantic 一致：同类型且字段值全部相等（定义 __eq__ 后实例不可哈希，
        # 这也是真 pydantic 的行为）。
        if type(other) is not type(self):
            return NotImplemented
        return self.__dict__ == other.__dict__

    # ------------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        fields = type(self)._resolve_fields()
        parts = ", ".join(
            f"{name}={self.__dict__.get(name, PydanticUndefined)!r}" for name in fields
        )
        return f"{type(self).__name__}({parts})"
