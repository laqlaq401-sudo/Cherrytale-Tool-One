"""AES 对称加解密（基于 cryptography 库的底层 hazmat 接口）。

【先搞懂三件事，后面就不会懵】

**1. 为什么会有「模式」**
AES 本身只负责把 **16 字节** 的明文块变成 16 字节密文块。要加密任意长度的数据，
就得把很多块串起来，串的方式就叫「模式」：

- ``ECB``：每块独立加密。**相同明文块 → 相同密文块**，安全性弱，
  但因为实现最简单，在手游协议里极其常见（尤其用于加密 protobuf 报文）。
- ``CBC``：需要 IV（初始向量）参与，块与块串联。比 ECB 安全，IV 通常随包一起发。
- ``CTR``：流式模式，不需要填充、长度随意，但依赖「绝不重复的计数器」。

**2. 为什么必须处理「填充」**
AES 只接受 16 字节整数倍。长度不够就得补齐，最主流的规则是 **PKCS#7**：
缺 n 个字节就补 n 个「值为 n」的字节，解密后看最后一个字节的值就知道要裁掉多少。
例：缺 3 字节 → 补 ``03 03 03``。

**3. 为什么不用 pycryptodome**
pycryptodome 最新版没有 Python 3.14 的预编译包，安装会失败（详见 requirements.txt 末尾）。
cryptography 官方支持 3.14 且 API 同样清晰，因此统一用它。
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from crypto.hash import BytesLike, to_bytes

#: AES 的块大小固定为 16 字节，这是算法本身决定的，不要改。
BLOCK_SIZE: Final[int] = 16

#: AES 允许的三档密钥长度（字节）：128 位 / 192 位 / 256 位。
VALID_KEY_LENGTHS: Final[frozenset[int]] = frozenset({16, 24, 32})


class AesError(ValueError):
    """AES 参数或数据不合法时抛出（例如密钥长度不对、密文不是 16 的倍数）。"""


# ---------------------------------------------------------------------------
# 填充（Padding）
# ---------------------------------------------------------------------------
def pkcs7_pad(data: BytesLike, block_size: int = BLOCK_SIZE) -> bytes:
    """PKCS#7 填充：把数据补到 ``block_size`` 的整数倍。

    注意一个反直觉的细节：**如果数据已经对齐，仍然要补满一整块**
    （16 个 ``0x10``）。否则解密方无法区分「原本就齐」和「补了 16 个」。
    """
    if not 1 <= block_size <= 255:
        raise AesError(f"block_size 必须在 1-255 之间，收到：{block_size}")
    raw = to_bytes(data)
    padding_length = block_size - (len(raw) % block_size)
    return raw + bytes([padding_length]) * padding_length


def pkcs7_unpad(data: BytesLike, block_size: int = BLOCK_SIZE) -> bytes:
    """去掉 PKCS#7 填充。

    :raises AesError: 填充非法时。最常见原因是密钥或 IV 用错了，
        解密出来是乱码，此时应该回头检查密钥，而不是继续往下算。
    """
    raw = to_bytes(data)
    if not raw:
        raise AesError("待去填充的数据为空")
    if len(raw) % block_size != 0:
        raise AesError(
            f"数据长度 {len(raw)} 不是块大小 {block_size} 的整数倍，密文可能不完整"
        )

    padding_length = raw[-1]
    if not 1 <= padding_length <= block_size:
        raise AesError(
            f"PKCS#7 填充非法：末字节为 {padding_length}，应在 1-{block_size} 之间。"
            "通常意味着密钥或 IV 用错了。"
        )
    if raw[-padding_length:] != bytes([padding_length]) * padding_length:
        raise AesError("PKCS#7 填充内容不一致，通常意味着密钥或 IV 用错了。")
    return raw[:-padding_length]


def zero_pad(data: BytesLike, block_size: int = BLOCK_SIZE) -> bytes:
    """零填充：用 ``0x00`` 补到整数倍。

    部分老游戏用这种更简单的方式（解密后靠截断或已知长度还原）。
    缺点是无法表达「末尾本来就是 0 的数据」。
    """
    raw = to_bytes(data)
    remainder = len(raw) % block_size
    if remainder == 0:
        return raw
    return raw + bytes(block_size - remainder)


def zero_unpad(data: BytesLike) -> bytes:
    """去掉零填充（移除尾部的 ``0x00``）。"""
    return to_bytes(data).rstrip(b"\x00")


# ---------------------------------------------------------------------------
# 内部校验工具
# ---------------------------------------------------------------------------
def _check_key(key: BytesLike) -> bytes:
    """校验并转换密钥。"""
    raw_key = to_bytes(key)
    if len(raw_key) not in VALID_KEY_LENGTHS:
        raise AesError(
            f"AES 密钥长度必须是 {sorted(VALID_KEY_LENGTHS)} 字节之一，"
            f"当前 {len(raw_key)} 字节。"
        )
    return raw_key


def _check_iv(iv: BytesLike) -> bytes:
    """校验并转换 IV（初始向量）。"""
    raw_iv = to_bytes(iv)
    if len(raw_iv) != BLOCK_SIZE:
        raise AesError(f"IV 必须是 {BLOCK_SIZE} 字节，当前 {len(raw_iv)} 字节。")
    return raw_iv


def _require_block_aligned(data: bytes, what: str) -> None:
    """ECB / CBC 要求数据长度是 16 的整数倍。"""
    if len(data) % BLOCK_SIZE != 0:
        raise AesError(
            f"{what}长度为 {len(data)}，不是 {BLOCK_SIZE} 的整数倍。"
            "请先做填充（pkcs7_pad），或改用 CTR 这类流式模式。"
        )


# ---------------------------------------------------------------------------
# ECB 模式
# ---------------------------------------------------------------------------
def aes_ecb_encrypt(
    data: BytesLike,
    key: BytesLike,
    *,
    use_padding: bool = True,
) -> bytes:
    """AES-ECB 加密。

    :param data: 明文（字符串会按 UTF-8 编码）。
    :param key: 16 / 24 / 32 字节密钥。
    :param use_padding: 是否自动做 PKCS#7 填充。
        逆向时若观察到密文长度恰好是 16 的倍数且没有填充痕迹，可试着设为 ``False``。
    """
    raw_key = _check_key(key)
    payload = pkcs7_pad(data) if use_padding else to_bytes(data)
    _require_block_aligned(payload, "明文")

    encryptor = Cipher(algorithms.AES(raw_key), modes.ECB()).encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def aes_ecb_decrypt(
    data: BytesLike,
    key: BytesLike,
    *,
    use_padding: bool = True,
) -> bytes:
    """AES-ECB 解密，参数含义同 :func:`aes_ecb_encrypt`。"""
    raw_key = _check_key(key)
    payload = to_bytes(data)
    _require_block_aligned(payload, "密文")

    decryptor = Cipher(algorithms.AES(raw_key), modes.ECB()).decryptor()
    plain = decryptor.update(payload) + decryptor.finalize()
    return pkcs7_unpad(plain) if use_padding else plain


# ---------------------------------------------------------------------------
# CBC 模式
# ---------------------------------------------------------------------------
def aes_cbc_encrypt(
    data: BytesLike,
    key: BytesLike,
    iv: BytesLike,
    *,
    use_padding: bool = True,
) -> bytes:
    """AES-CBC 加密。

    :param iv: 16 字节初始向量。同一密钥下 **IV 绝不应重复**，
        否则相同明文会产出相同密文。逆向时 IV 通常有两种来源：
        固定常量，或随机生成后放在报文头部一起发。
    """
    raw_key = _check_key(key)
    raw_iv = _check_iv(iv)
    payload = pkcs7_pad(data) if use_padding else to_bytes(data)
    _require_block_aligned(payload, "明文")

    encryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_iv)).encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def aes_cbc_decrypt(
    data: BytesLike,
    key: BytesLike,
    iv: BytesLike,
    *,
    use_padding: bool = True,
) -> bytes:
    """AES-CBC 解密，参数含义同 :func:`aes_cbc_encrypt`。"""
    raw_key = _check_key(key)
    raw_iv = _check_iv(iv)
    payload = to_bytes(data)
    _require_block_aligned(payload, "密文")

    decryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_iv)).decryptor()
    plain = decryptor.update(payload) + decryptor.finalize()
    return pkcs7_unpad(plain) if use_padding else plain


# ---------------------------------------------------------------------------
# CTR 模式（流式，无需填充）
# ---------------------------------------------------------------------------
def aes_ctr_xor(data: BytesLike, key: BytesLike, nonce: BytesLike) -> bytes:
    """AES-CTR 加解密（加与解是同一个操作，调本函数两次即还原）。

    :param nonce: 16 字节的初始计数器值。

    CTR 把 AES 当成「密钥流生成器」，明文与密钥流异或即可，
    因此 **不需要填充**，任意长度都能处理。
    """
    raw_key = _check_key(key)
    raw_nonce = _check_iv(nonce)  # CTR 的初始计数器同样必须等于块大小

    transformer = Cipher(algorithms.AES(raw_key), modes.CTR(raw_nonce)).encryptor()
    return transformer.update(to_bytes(data)) + transformer.finalize()

