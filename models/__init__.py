"""models 包：根据 dump.cs 还原的协议数据结构。

已知事实（来自对 dump.cs 的检索与实测）：

- ``com.auer.game.protobuf`` 命名空间下有 **1098 个协议类**。
  命名规律是「请求 = ``XxxPacket``、响应 = ``XxxRes``」
  （例：``LoginPacket`` / ``LoginRes``、``DMReward`` / ``DMRewardRes``）——
  **不是** ``Req`` / ``Res``，这一点在写模型时很容易搞反；
- 外层封包是 ``RootPacket``（TypeDefIndex 9233）：
  ``packetID`` + ``token`` + ``playerId`` + ``timeStampToken`` + ``timeStampClient``
  + ``vCode``（签名），再挂上具体的业务子包；
- 字段的内存偏移也会一并给出（如 ``// 0x10``），可用于交叉验证字段顺序。

【本包的四个层次（职责严格分开）】

1. **基类**：``base.py`` 的 ``ProtocolModel``
   （字段校验 / 别名映射 / 序列化 / 调试打码）；
2. **生成表**（由 ``tools/`` 下的脚本从 dump.cs 自动生成，**禁止手改**）：

   - ``packet_ids.py``：868 个「协议类名 → 消息号」，发包时唯一需要查的表；
   - ``const_ids.py``：``RPS_Const`` 的 672 个常量（金币 / 钻石等资源 ID）；
   - ``proto_fields.py``：各报文的**字段顺序**（= protobuf 字段编号依据）；

3. **本地配置表**：``config_table.py``（解析 ``|`` 分隔、``┤`` 结尾的文本表）
   + ``game_config.py``（日常任务表的业务语义）；
4. **报文编解码**：``protobuf_wire.py``（wire format 原语，零依赖实现）
   + ``game_packet.py``（``ProtoMessage`` 基类，按字段类型自动编解码）
   + ``daily.py`` / ``handshake.py`` 等具体报文模块；
5. **信封层**：``envelope.py`` —— 把报文装进 ``RootPacket``。
   实测要点：前 16 个元信息字段是顺序编号，而**业务子包槽位的编号 = 该子包的消息号**。

⚠️ 仍然**不应凭猜测**的部分（靠抓包确认）：

- 尚未在抓包里出现过的报文（例如 6001 日常任务列表）的字段编号，
  仍按「编号 = 声明顺序」推断 —— 这条规则已由 ``VersionControlServerPacket``
  实测证实，但每个新报文第一次使用时都建议用 ``describe_message()`` 对照一次；
- 各接口的成功 / 错误码取值。

【维护命令】

    python tools/extract_packet_ids.py --target all    # 重新生成消息号表与常量表
    python tools/extract_proto_fields.py               # 重新生成报文字段表
    python main.py selftest                            # 离线体检（含生成表是否过期）
"""

__all__: list[str] = []
