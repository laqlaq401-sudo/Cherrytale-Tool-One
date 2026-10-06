"""crypto 包：签名字符串拼装、哈希校验、AES/XOR 加解密与 Token 封装。

本包当前提供的是**通用加密原语工具箱**（哈希、异或、AES、HMAC）。
这些原语的正确性可以用标准测试向量独立验证，与游戏协议无关。

⚠️ 与游戏强相关的部分（签名 Salt、参数排序规则、密钥派生方式）目前是占位实现，
   必须等 dump.cs 中 com.auer.game.protobuf 命名空间与相关 Crypto 类分析完成后填入，
   详见 crypto/sign.py 中的 TODO(protocol) 标记。
"""

__all__: list[str] = []
