"""协议样本库 —— 真实抓包字节的**生产侧**存放点。

【为什么不在 tests/ 里】
这些样本此前放在 ``protocol_samples/``，造成一个方向倒置的依赖：

    tools/show_servers.py  ──→  protocol_samples/gateway_samples.py
            ↑                                   │
            └──────────  tests/test_server.py ───┘

生产工具（``tools/``）去 import 测试目录，于是「删掉 tests/」会让生产代码崩掉 ——
这违反了「测试应当可以与运行完全剥离」的原则。

搬到顶层包之后依赖方向理顺为：

    生产代码（client / models / tasks / services）
        └──→ protocol_samples/          （协议实测锚点，本就属于生产资产）
    tools/  ──→ protocol_samples/       （工具读取样本）
    tests/  ──→ 生产代码 + protocol_samples/   （测试验证两者）

此时删掉整个 ``tests/`` 目录，``main.py`` 与 ``tools/`` **不受任何影响**。

【这里放什么】
- ``gateway_samples.py`` —— 由 ``tools/extract_har.py`` 自动生成，**请勿手工编辑**
- ``session_samples.py`` —— 手工维护（来自 HAR 之外，如 Fiddler HexView 复制）
- 其余 ``*_samples.py`` —— 各业务链路的报文样本

【重要提醒】
样本内嵌**真实报文字节**，其中的身份字段（userId / deviceID）**已脱敏为占位符**。
若需真实值，请自行从 ``captures/`` 重新导出 —— 但注意脱敏后的占位符会改变
报文长度，因此依赖「逐字节等长」的断言以常量形式独立声明（见 session_samples.py）。
"""
