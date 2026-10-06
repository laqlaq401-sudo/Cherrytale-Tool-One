"""tests 包：各独立模块的最小可运行单元测试。

这个 ``__init__.py`` 不是可有可无的装饰，它有两个实际作用：

1. **让模块方式运行生效**：有了它，才能用
       python -m tests.test_crypto
   直接单跑某一个测试文件（``python -m`` 会把当前目录加入导入路径，
   这样 ``import crypto.hash`` 才找得到）。
2. **避免文件重名冲突**：未来可能新增更多测试文件，包结构能防止 pytest
   在导入同名模块时互相覆盖。

运行全部测试：
    python -m pytest tests -q
"""

__all__: list[str] = []
