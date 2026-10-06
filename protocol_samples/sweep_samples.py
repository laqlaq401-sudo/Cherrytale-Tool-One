"""降临／活动关卡扫荡的真实抓包样本（**从 captures 导出，请勿手工编辑**）。

【这批样本解决什么问题】
协议逆向上最容易犯的错，是"按 dump.cs 的字段清单推断出编号，然后直接拿它组包"。
编号差一位，服务端会安静地把值读到别的字段上 —— 现象是"参数看着没错但行为怪"。

所以本文件把 ``captures/降临扫荡抓包1.saz`` 里**扫荡链路的关键字节**固化成常量，
让 ``tests/test_activity_stage.py`` 能在没有抓包文件的环境里也做**字节级**验证。

【数据来源】
``captures/降临扫荡抓包1.saz``（2026-09-21，用户实测：扫荡 1 次 + 点 5 次被削成 4 次）。
下面全部是**子包字节**（已剥掉 ``RootPacket`` 信封），也就是 ``ProtoMessage.decode()``
直接吃的那一段；这样测试就不必依赖信封解析。

【导出命令（如需更新）】
    PYTHONIOENCODING=utf-8 python -c "import glob,zipfile,sys;sys.path.insert(0,'.');\\
from models.envelope import parse_envelope as P;from models.protobuf_wire import fields_by_number as F;\\
z=zipfile.ZipFile([f for f in glob.glob('captures/*.saz') if '降' in f][0]);\\
S=lambda n:P(z.read(n+'_s.txt').split(b'\\r\\n\\r\\n',1)[1]).sub_packet_bytes;\\
Q=lambda n:P(z.read(n+'_c.txt').split(b'\\r\\n\\r\\n',1)[1]).sub_packet_bytes;\\
g=F(S('raw/02'));h=F(S('raw/03'));print('REQ08',Q('raw/08').hex());print('REQ10',Q('raw/10').hex());\\
print('RES08',S('raw/08').hex());print('RES10',S('raw/10').hex());\\
print('AREA',g[1][0].as_bytes().hex());print('SEC',h[1][0].as_bytes().hex())"
"""

from __future__ import annotations

from typing import Final

#: 扫荡请求（raw/08，**扫 1 次**）：sectionID=129110539, times=1, useSweepTicket=1
SWEEP_REQUEST_ONCE_HEX: Final[str] = "088ba4c83d10011801"

#: 扫荡请求（raw/10，用户点 5 次、被客户端削成 **4 次**）：times=4
SWEEP_REQUEST_FOUR_HEX: Final[str] = "088ba4c83d10041801"

#: 扫荡响应（raw/08）：errorCode=0；体力→160；扫荡券→893；奖励 1 条明细
SWEEP_RESPONSE_ONCE_HEX: Final[str] = (
    "080012310a0e08b790a2bc0210b3aec15f189a080a0f08e3f1c07810b284af5f18d1e9d009"
    "0a0e08dff1c078109184af5f18bda25812001a1e0a0d08ddf1c078108284af5f18a0010a0d"
    "08d7e5cb78109084af5f18fd06"
)

#: 扫荡响应（raw/10）：体力→0；扫荡券→889（-4 = times）；奖励明细 4 条（数量逐次递增）
SWEEP_RESPONSE_FOUR_HEX: Final[str] = (
    "080012310a0e08b790a2bc0210b3aec15f18aa0b0a0f08e3f1c07810b284af5f18a1f9d009"
    "0a0e08dff1c078109184af5f18bda25812310a0e08b790a2bc0210b3aec15f18ba0e0a0f08e3"
    "f1c07810b284af5f18f188d1090a0e08dff1c078109184af5f18bda25812310a0e08b790a2bc"
    "0210b3aec15f18ca110a0f08e3f1c07810b284af5f18c198d1090a0e08dff1c078109184af5f"
    "18bda25812310a0e08b790a2bc0210b3aec15f18da140a0f08e3f1c07810b284af5f1891a8d1"
    "090a0e08dff1c078109184af5f18bda25812001a1d0a0c08ddf1c078108284af5f18000a0d08"
    "d7e5cb78109084af5f18f906"
)

#: ``11026`` 的第一个区域元素（raw/02 的 areaStateList[0]）：areaID=128010023、
#: 进度 ``5/5``、weeks=2、起止时间都是毫秒时间戳
AREA_ELEMENT_HEX: Final[str] = (
    "08a78e853d10001a2108ffffffffffffffffff0110ffffffffffffffffff0118ffffffffff"
    "ffffffff0122274c6576656c5f41637469766974795f42616e6e65725f4441524b5f506f72"
    "74616c5f502e706e67288095b79d88343080a59bde8c343a2b4c6576656c5f4163746976"
    "6974795f4d61696e42616e6e65725f4441524b5f506f7274616c5f502e706e67400f4801"
    "4801480150ffffffffffffffffff0158026203352f3568007000"
)

#: ``11004`` 的第一个关卡元素（raw/03 的 sectionStateList[0]）：sectionID=129110531、
#: sectionState=2（全部 9 关都是 2，语义未确认）
SECTION_ELEMENT_HEX: Final[str] = (
    "0883a4c83d10021801180118012000280130feffffffffffffffff013800422108ffffffff"
    "ffffffffff0110ffffffffffffffffff0118ffffffffffffffffff0148ffffffffffffffff"
    "ff015000580060ffffffffffffffffff01"
)
