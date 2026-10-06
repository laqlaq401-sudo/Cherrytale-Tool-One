"""异或（XOR）加解密与单字节密钥爆破工具。

【为什么需要它】
XOR 是对称加密里最小的那颗螺丝：**同样的操作做两遍就还原**
（因为 ``A ⊕ B ⊕ B == A``）。很多轻量手游协议用它做一层混淆，
或者把明文和密钥流逐字节混合。它不安全，只是混淆，但在逆向里极常见。

【核心概念：密钥循环使用】
密钥通常比数据短，所以规则是「密钥从头到尾循环」：

    数据:   01 02 03 04 05
    密钥:   AB CD
    实际用: AB CD AB CD AB
    结果:   AA CF A8 C8 AE

本模块默认就是这种「重复密钥异或」行为。

⚠️ 逆向提醒：如果发现密钥是游戏内的动态值（例如某个 sessionId 的哈希），
   那属于业务规则，应放到 crypto/sign.py 并加 TODO(protocol) 说明，不要塞在这里。
"""

from __future__ import annotations

from typing import Final

from crypto.hash import BytesLike, to_bytes

#: 用于给「明文打分」的英文字母频率表（百分比，来源为通用英文语料统计）。
#: 只用来在爆破时给候选结果排序，不需要极其精确。
_ENGLISH_LETTER_FREQ: Final[dict[int, float]] = {
    ord("a"): 8.17, ord("b"): 1.49, ord("c"): 2.78, ord("d"): 4.25,
    ord("e"): 12.70, ord("f"): 2.23, ord("g"): 2.02, ord("h"): 6.09,
    ord("i"): 6.97, ord("j"): 0.15, ord("k"): 0.77, ord("l"): 4.03,
    ord("m"): 2.41, ord("n"): 6.75, ord("o"): 7.51, ord("p"): 1.93,
    ord("q"): 0.10, ord("r"): 5.99, ord("s"): 6.33, ord("t"): 9.06,
    ord("u"): 2.76, ord("v"): 0.98, ord("w"): 2.36, ord("x"): 0.15,
    ord("y"): 1.97, ord("z"): 0.07,
}

#: 被视为「可见字符」的字节范围（含常见空白与控制符）
_PRINTABLE_EXTRA: Final[frozenset[int]] = frozenset({9, 10, 13})


def xor_bytes(data: BytesLike, key: BytesLike) -> bytes:
    """用循环密钥对数据做异或。

    :param data: 待处理数据（字符串会按 UTF-8 编码）。
    :param key: 密钥。**不能为空**，否则无法循环取值。
    :return: 与 ``data`` 等长的新字节串。
    :raises ValueError: 密钥为空时（空密钥会让异或失去意义，属于调用方错误）。

    提示：异或是对称的，加密与解密都调用这同一个函数。
    """
    raw_data = to_bytes(data)
    raw_key = to_bytes(key)
    if not raw_key:
        raise ValueError("密钥不能为空：空密钥会导致数据保持原样，属于调用错误")

    key_length = len(raw_key)
    # 用取模实现「密钥循环」；用 bytes(...) 一次性构造，比 += 拼接快得多。
    return bytes(
        byte ^ raw_key[index % key_length] for index, byte in enumerate(raw_data)
    )


def xor_hex(data: BytesLike, key: BytesLike) -> str:
    """异或后返回小写十六进制字符串（方便和抓包工具里的十六进制对照）。"""
    return xor_bytes(data, key).hex()


def xor_single_byte(data: BytesLike, key_byte: int) -> bytes:
    """用**单个字节**做异或（最常见的简易混淆形式）。

    :param key_byte: 0-255 之间的整数。
    :raises ValueError: 取值越界时。
    """
    if not 0 <= key_byte <= 0xFF:
        raise ValueError(f"key_byte 必须在 0-255 之间，收到：{key_byte}")
    return xor_bytes(data, bytes([key_byte]))


def score_plaintext(data: bytes) -> float:
    """给一段字节「像不像可读文本」打分，分数越高越像。

    评分由两部分组成：

    1. **可打印字符占比**（权重高）：真正的明文几乎全是可打印字符；
    2. **英文高频字母加成**：让 ``the``、``player`` 这类文本排在乱码前面。

    这是逆向里爆破密钥时的经验做法，不需要精确，只要能把正确答案顶到前面。
    """
    if not data:
        return 0.0

    printable_count = sum(
        1 for byte in data if 32 <= byte <= 126 or byte in _PRINTABLE_EXTRA
    )
    printable_ratio = printable_count / len(data)

    letter_score = sum(
        _ENGLISH_LETTER_FREQ.get(byte | 0x20, 0.0) for byte in data
    ) / len(data)

    return printable_ratio * 100.0 + letter_score


def crack_single_byte_xor(
    ciphertext: BytesLike,
    top: int = 5,
) -> list[tuple[int, float]]:
    """爆破单字节异或密钥，返回最可能的若干候选。

    :param ciphertext: 密文。
    :param top: 返回前几名候选。
    :return: 形如 ``[(key_byte, score), ...]`` 的列表，按可能性从高到低排列。

    用法示例（拿到一段疑似被单字节异或混淆的字节时）::

        for key, score in crack_single_byte_xor(blob, top=3):
            print(key, hex(key), score, xor_single_byte(blob, key))

    ⚠️ 注意：这是**启发式**方法。如果明文不是英文/可打印文本
    （例如是 protobuf 二进制或已压缩数据），评分会失效，
    此时应改用「已知明文攻击」：拿你确定的一段明文去和密文异或，直接还原密钥。
    """
    raw = to_bytes(ciphertext)
    candidates = [
        (key, score_plaintext(xor_single_byte(raw, key))) for key in range(256)
    ]
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[:top]


def known_plaintext_key(ciphertext: BytesLike, plaintext: BytesLike) -> bytes:
    """已知明文攻击：由「密文 + 对应明文」直接算出异或密钥流。

    原理就是 XOR 的自反性：``密文 ⊕ 明文 == 密钥``。

    :return: 长度等于两者较短者的密钥字节。
    :raises ValueError: 两者都为空，或长度不相同时（长度不同说明样本对不上）。
    """
    raw_cipher = to_bytes(ciphertext)
    raw_plain = to_bytes(plaintext)
    if not raw_cipher or not raw_plain:
        raise ValueError("密文与明文都不能为空")
    if len(raw_cipher) != len(raw_plain):
        raise ValueError(
            f"密文({len(raw_cipher)} 字节)与明文({len(raw_plain)} 字节)长度必须一致"
        )
    return bytes(a ^ b for a, b in zip(raw_cipher, raw_plain, strict=True))
