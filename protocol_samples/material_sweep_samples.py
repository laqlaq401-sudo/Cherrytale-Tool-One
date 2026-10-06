"""素材关卡扫荡.saz 的真实抓包样本（**由 tools/export_saz_samples.py 自动生成，请勿手工编辑**）。

【数据来源】
``captures/素材关卡扫荡.saz``（用户实测导出；原始 SAZ 仍在 captures/ 里）。

【怎么用】
在你自己的测试里 import 下面这些常量（全部是**子包字节**）。

【导出命令（如需更新，原样重跑即可）】
    PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py --match '素材关卡' --sessions raw/08 --prefix MSWEEP --out protocol_samples/material_sweep_samples.py

【为什么存子包字节】
``ProtoMessage.decode()`` 直接吃这段（已剥掉 ``RootPacket`` 信封），
这样测试不依赖信封解析，也不会被 HTTP 头干扰。
"""

from __future__ import annotations

from typing import Final

#: raw/08 request：SweepSectionPacket(11009)，子包 9 B
MSWEEP08_REQUEST_HEX: Final[str] = "088893c23d10051800"

#: raw/08 response：SweepSectionRes(11010)，子包 261 B
MSWEEP08_RESPONSE_HEX: Final[str] = (
    "080012310a0e08eacedfa1011090aec15f18a8080a0f08e3f1c07810b284af5f"
    "1895ecc8090a0e08dff1c078109184af5f18bda25812310a0e08eacedfa10110"
    "90aec15f18b2080a0f08e3f1c07810b284af5f18c1eec8090a0e08dff1c07810"
    "9184af5f18bda25812310a0e08eacedfa1011090aec15f18bc080a0f08e3f1c0"
    "7810b284af5f18edf0c8090a0e08dff1c078109184af5f18bda25812310a0e08"
    "eacedfa1011090aec15f18c6080a0f08e3f1c07810b284af5f1899f3c8090a0e"
    "08dff1c078109184af5f18bda25812310a0e08eacedfa1011090aec15f18d008"
    "0a0f08e3f1c07810b284af5f18c5f5c8090a0e08dff1c078109184af5f18bda2"
    "5812001a00"
)
