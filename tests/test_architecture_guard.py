"""架构合规性与发布安全护栏测试（Architecture Guardrails）。

【红线守则】
PyInstaller 打包规范（CherrytaleTool.spec）中明确排除了开发工具目录：
    excludes = ['tools', 'pytest', 'tests', ...]
因此，所有运行期生产代码绝对严禁直接或间接 import tools.*，
否则打包发布的 EXE 或 APK 运行时必然抛出 ModuleNotFoundError: No module named 'tools' 导致崩溃。

本测试自动化遍历所有生产运行时目录，使用 AST 语法树静态分析所有 import 语句，
从 CI/测试期彻底杜绝将非法依赖带入打包发布产物。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: 必须保持 100% 纯净、严禁依赖 tools 的生产运行时源码目录
PRODUCTION_DIRS = (
    "client",
    "crypto",
    "models",
    "services",
    "tasks",
    "webapi",
)


def _collect_tools_imports(py_file: Path) -> list[tuple[int, str]]:
    """使用 AST 语法树解析指定 Python 文件中的 tools 导入。

    :return: [(行号, 导入语句描述), ...]
    """
    try:
        content = py_file.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(py_file))
    except Exception as exc:
        pytest.fail(f"无法解析文件语法树 {py_file}: {exc}")

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tools" or alias.name.startswith("tools."):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "tools" or node.module.startswith("tools.")):
                names = ", ".join(a.name for a in node.names)
                violations.append((node.lineno, f"from {node.module} import {names}"))

    return violations


def test_production_modules_must_not_import_tools() -> None:
    """断言所有运行时生产模块（tasks/client/models/crypto/services/webapi）绝无 tools 依赖。"""
    project_root = Path(__file__).resolve().parent.parent
    violations_summary: list[str] = []

    for dir_name in PRODUCTION_DIRS:
        target_dir = project_root / dir_name
        if not target_dir.is_dir():
            continue

        for py_file in target_dir.rglob("*.py"):
            # 跳过 pycache
            if "__pycache__" in py_file.parts:
                continue

            violations = _collect_tools_imports(py_file)
            for lineno, stmt in violations:
                rel_path = py_file.relative_to(project_root)
                violations_summary.append(f"  - {rel_path}:{lineno} -> {stmt}")

    if violations_summary:
        msg = (
            "\n[FATAL] 生产核心模块违规导入了 tools 开发目录！\n"
            "CherrytaleTool.spec 打包时已通过 excludes=['tools'] 将其排除，\n"
            "此类导入将直接导致发布版 EXE/APK 运行时闪退或登录报错（ModuleNotFoundError: No module named 'tools'）。\n"
            "违规代码清单：\n"
            + "\n".join(violations_summary)
            + "\n\n修复指引：\n"
            "  1. 加密/哈希/Token/签名算法 -> 请迁移至 crypto/ 模块\n"
            "  2. 通信/网络协议 -> 请归入 client/ 或 models/\n"
            "  3. 业务流与任务逻辑 -> 请归入 tasks/\n"
        )
        pytest.fail(msg)


def test_android_python_must_not_import_tools() -> None:
    """断言 Android 工程内的 Python 源码目录也绝无 tools 依赖。"""
    project_root = Path(__file__).resolve().parent.parent
    android_py_dir = project_root / "android" / "app" / "src" / "main" / "python"
    if not android_py_dir.is_dir():
        pytest.skip("Android python 源码目录不存在，跳过")

    violations_summary: list[str] = []
    for dir_name in PRODUCTION_DIRS:
        target_dir = android_py_dir / dir_name
        if not target_dir.is_dir():
            continue

        for py_file in target_dir.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue

            violations = _collect_tools_imports(py_file)
            for lineno, stmt in violations:
                rel_path = py_file.relative_to(project_root)
                violations_summary.append(f"  - {rel_path}:{lineno} -> {stmt}")

    if violations_summary:
        pytest.fail(
            "Android Python 生产代码违规导入了 tools：\n"
            + "\n".join(violations_summary)
        )
