"""crypto 包单元测试：哈希 / 异或 / AES / 签名。

运行方式（二选一，效果相同）：

    python -m tests.test_crypto        # 当脚本直接跑，输出更详细
    python -m pytest tests/test_crypto.py -q

【测试思路说明（初学者重点看这里）】

哈希和 AES 的正确性**不能靠「跑通了」来判断** —— 加密结果就算算错一位，
程序照样「跑通」。所以这里全部使用**权威测试向量**：

- MD5 / SHA / HMAC 使用公认的标准输入输出对照；
- AES 使用美国国家标准 FIPS-197 附录中的官方测试向量。

只要这些向量对得上，就证明我们的实现真的符合标准，而不是自说自话。
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

import config
from crypto.hash import (
    constant_time_compare,
    file_hash_hex,
    hash_hex,
    hmac_hex,
    md5_hex,
    sha1_hex,
    sha256_hex,
    to_bytes,
)
from crypto.xor import (
    crack_single_byte_xor,
    known_plaintext_key,
    score_plaintext,
    xor_bytes,
    xor_hex,
    xor_single_byte,
)
from crypto.aes import (
    AesError,
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    aes_ctr_xor,
    aes_ecb_decrypt,
    aes_ecb_encrypt,
    pkcs7_pad,
    pkcs7_unpad,
    zero_pad,
    zero_unpad,
)
from crypto.sign import (
    SignError,
    TokenStore,
    build_sign_base_string,
    sign_request,
    sign_with_salt,
    verify_signature,
)

# ---------------------------------------------------------------------------
# crypto.hash
# ---------------------------------------------------------------------------
class TestHash:
    """哈希模块：全部以标准测试向量为准。"""

    def test_to_bytes_string_uses_utf8(self) -> None:
        """中文按 UTF-8 编码为 3 字节，这是协议里的默认约定。"""
        assert to_bytes("中") == b"\xe4\xb8\xad"

    def test_to_bytes_bytes_passthrough(self) -> None:
        assert to_bytes(b"abc") == b"abc"

    def test_to_bytes_bytearray_converted(self) -> None:
        assert to_bytes(bytearray(b"abc")) == b"abc"

    def test_to_bytes_rejects_int(self) -> None:
        """传整数应给出明确异常，而不是悄悄产生奇怪结果。"""
        with pytest.raises(TypeError, match="不支持的数据类型"):
            to_bytes(123)  # type: ignore[arg-type]

    def test_md5_known_vector(self) -> None:
        assert md5_hex("abc") == "900150983cd24fb0d6963f7d28e17f72"

    def test_sha1_known_vector(self) -> None:
        assert sha1_hex("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"

    def test_sha256_known_vector(self) -> None:
        assert (
            sha256_hex("abc")
            == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_hex_output_is_lowercase(self) -> None:
        """统一小写，避免服务端大小写敏感导致的诡异失败。"""
        assert md5_hex("abc").islower()

    def test_str_and_bytes_input_equivalent(self) -> None:
        assert md5_hex("abc") == md5_hex(b"abc")

    def test_unknown_algorithm_raises(self) -> None:
        with pytest.raises(ValueError, match="不支持的哈希算法"):
            hash_hex("abc", "md4")  # type: ignore[arg-type]

    def test_hmac_md5_known_vector(self) -> None:
        """标准用例：key="key"，data="The quick brown fox jumps over the lazy dog"。"""
        assert (
            hmac_hex("key", "The quick brown fox jumps over the lazy dog", "md5")
            == "80070713463e7749b90c2dc24911e275"
        )

    def test_hmac_matches_stdlib(self) -> None:
        """再和 Python 标准库的 hmac 对一次，双保险。"""
        expected = hmac.new(b"k", b"data", hashlib.sha256).hexdigest()
        assert hmac_hex("k", "data", "sha256") == expected

    def test_file_hash_matches_stdlib(self) -> None:
        """文件哈希应与「整文件读入再算」一致，证明分块读取没问题。"""
        target: Path = config.PROJECT_ROOT / "requirements.txt"
        expected = hashlib.md5(target.read_bytes()).hexdigest()
        assert file_hash_hex(target) == expected

    def test_file_hash_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            file_hash_hex(config.PROJECT_ROOT / "这个文件肯定不存在.txt")

    def test_constant_time_compare_equal(self) -> None:
        assert constant_time_compare("abc", "abc") is True

    def test_constant_time_compare_different(self) -> None:
        assert constant_time_compare("abc", "abd") is False

    def test_constant_time_compare_mixed_types(self) -> None:
        """字符串与字节内容相同也应判为相等（内部统一编码后比较）。"""
        assert constant_time_compare(b"abc", "abc") is True

    def test_constant_time_compare_invalid_type(self) -> None:
        """不可转换的类型不应抛异常，直接判为不相等。"""
        assert constant_time_compare(1, 1) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# crypto.xor
# ---------------------------------------------------------------------------
class TestXor:
    """异或模块：往返性、密钥循环、爆破与已知明文攻击。"""

    def test_roundtrip(self) -> None:
        """异或两次等于没异或。"""
        assert xor_bytes(xor_bytes(b"hello", "key"), "key") == b"hello"

    def test_key_is_repeated(self) -> None:
        """密钥长度不足时应从头循环使用。"""
        data = bytes([0x01, 0x02, 0x03, 0x04, 0x05])
        key = bytes([0xAB, 0xCD])
        expected = bytes(
            [0x01 ^ 0xAB, 0x02 ^ 0xCD, 0x03 ^ 0xAB, 0x04 ^ 0xCD, 0x05 ^ 0xAB]
        )
        assert xor_bytes(data, key) == expected

    def test_length_preserved(self) -> None:
        assert len(xor_bytes(b"x" * 37, b"k")) == 37

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="密钥不能为空"):
            xor_bytes(b"data", b"")

    def test_hex_output(self) -> None:
        assert xor_hex(b"\x00\xff", b"\xff") == "ff00"

    def test_single_byte_roundtrip(self) -> None:
        original = b"protobuf payload \x00\x01\x02"
        assert xor_single_byte(xor_single_byte(original, 0x5A), 0x5A) == original

    def test_single_byte_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="0-255"):
            xor_single_byte(b"data", 256)

    def test_score_prefers_readable_text(self) -> None:
        """可读文本得分必须高于随机字节，这是爆破排序的基础。"""
        assert score_plaintext(b"the player attack damage is 100") > score_plaintext(
            bytes(range(256))
        )

    def test_score_empty_is_zero(self) -> None:
        assert score_plaintext(b"") == 0.0

    def test_crack_single_byte_xor_finds_key(self) -> None:
        """已知密钥 0x5A，爆破结果第一名必须是它。"""
        plaintext = b"this is a secret message from the game server"
        ciphertext = xor_single_byte(plaintext, 0x5A)
        assert crack_single_byte_xor(ciphertext, top=1)[0][0] == 0x5A

    def test_crack_returns_requested_count(self) -> None:
        assert len(crack_single_byte_xor(b"\x01\x02\x03", top=3)) == 3

    def test_known_plaintext_attack(self) -> None:
        """用「密文 + 明文」样本直接还原密钥流。

        注意返回值长度等于**报文长度**（这里是 16 字节），
        而不是「密钥重复整数次」：
        3 字节密钥循环到第 16 字节时，只会用到它的第 1 个字节（``0x11``）。
        """
        plaintext = b'{"uid":"123456"}'  # 16 字节
        key = b"\x11\x22\x33"  # 3 字节，循环使用
        ciphertext = xor_bytes(plaintext, key)

        recovered = known_plaintext_key(ciphertext, plaintext)
        expected = bytes(key[index % len(key)] for index in range(len(plaintext)))

        assert recovered == expected
        assert len(recovered) == len(plaintext)

    def test_known_plaintext_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="长度必须一致"):
            known_plaintext_key(b"abc", b"abcd")


# ---------------------------------------------------------------------------
# crypto.aes —— 填充
# ---------------------------------------------------------------------------
class TestAesPadding:
    """填充规则是 AES 最容易出错的地方，单独测。"""

    def test_pad_short_data(self) -> None:
        """缺 13 字节就补 13 个 0x0D。"""
        assert pkcs7_pad(b"abc") == b"abc" + b"\x0d" * 13

    def test_pad_exact_block_adds_full_block(self) -> None:
        """已经对齐时仍要补满一整块，否则解密方无法判断该裁多少。"""
        assert pkcs7_pad(b"a" * 16) == b"a" * 16 + b"\x10" * 16

    @pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 100])
    def test_pad_unpad_roundtrip(self, size: int) -> None:
        data = b"x" * size
        assert pkcs7_unpad(pkcs7_pad(data)) == data

    def test_unpad_rejects_bad_padding_byte(self) -> None:
        """末字节为 0xFF 明显非法（超过块大小 16）。"""
        with pytest.raises(AesError, match="填充非法"):
            pkcs7_unpad(b"a" * 15 + b"\xff")

    def test_unpad_rejects_unaligned_length(self) -> None:
        with pytest.raises(AesError, match="整数倍"):
            pkcs7_unpad(b"abc")

    def test_unpad_rejects_inconsistent_padding(self) -> None:
        """末字节说是 3 个，但倒数第三字节不是 0x03，说明密钥用错了。"""
        with pytest.raises(AesError, match="不一致"):
            pkcs7_unpad(b"a" * 13 + b"\x01\x02\x03")

    def test_zero_pad_partial_block(self) -> None:
        assert zero_pad(b"abc") == b"abc" + b"\x00" * 13

    def test_zero_pad_aligned_unchanged(self) -> None:
        """零填充与 PKCS#7 不同：已经对齐就**不**补。"""
        assert zero_pad(b"a" * 16) == b"a" * 16

    def test_zero_unpad(self) -> None:
        assert zero_unpad(b"abc\x00\x00") == b"abc"


# ---------------------------------------------------------------------------
# crypto.aes —— 加密
# ---------------------------------------------------------------------------
class TestAesCipher:
    """AES 加解密：以 FIPS-197 官方测试向量为准。"""

    #: 16 字节密钥：00 01 02 ... 0F（即 128 位密钥）
    KEY_128: bytes = bytes(range(16))
    #: 另一把密钥，用于验证「密钥错了解不出来」
    OTHER_KEY: bytes = bytes(range(16, 32))
    #: 16 字节 IV
    IV: bytes = bytes(range(16))

    def test_fips197_ecb_vector(self) -> None:
        """FIPS-197 附录 B 官方向量：

        密钥 000102030405060708090A0B0C0D0E0F
        明文 00112233445566778899AABBCCDDEEFF
        密文 69C4E0D86A7B0430D8CDB78070B4C55A
        """
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected = "69c4e0d86a7b0430d8cdb78070b4c55a"

        assert aes_ecb_encrypt(plaintext, self.KEY_128, use_padding=False).hex() == expected
        assert (
            aes_ecb_decrypt(bytes.fromhex(expected), self.KEY_128, use_padding=False)
            == plaintext
        )

    def test_ecb_roundtrip_utf8_chinese(self) -> None:
        text = "领奖励：每日任务 × 3 ✅"
        restored = aes_ecb_decrypt(aes_ecb_encrypt(text, self.KEY_128), self.KEY_128)
        assert restored.decode("utf-8") == text

    def test_ecb_is_deterministic(self) -> None:
        """ECB 的特点：相同明文块永远得到相同密文块（这也正是它不安全的原因）。"""
        first = aes_ecb_encrypt(b"same content", self.KEY_128)
        second = aes_ecb_encrypt(b"same content", self.KEY_128)
        assert first == second

    def test_ecb_ciphertext_is_block_aligned(self) -> None:
        assert len(aes_ecb_encrypt(b"x", self.KEY_128)) % 16 == 0

    def test_cbc_roundtrip(self) -> None:
        text = "CBC 往返测试 with mixed 中文 and ASCII"
        restored = aes_cbc_decrypt(
            aes_cbc_encrypt(text, self.KEY_128, self.IV), self.KEY_128, self.IV
        )
        assert restored.decode("utf-8") == text

    def test_cbc_different_iv_gives_different_ciphertext(self) -> None:
        """IV 的作用：同一密钥下换 IV，密文就不同，避免「相同明文->相同密文」。"""
        data = "identical plaintext"
        other_iv = bytes(range(1, 17))
        cipher_a = aes_cbc_encrypt(data, self.KEY_128, self.IV)
        cipher_b = aes_cbc_encrypt(data, self.KEY_128, other_iv)
        assert cipher_a != cipher_b

    def test_cbc_without_padding_requires_alignment(self) -> None:
        payload = b"y" * 32
        cipher = aes_cbc_encrypt(payload, self.KEY_128, self.IV, use_padding=False)
        assert aes_cbc_decrypt(cipher, self.KEY_128, self.IV, use_padding=False) == payload

    def test_ctr_does_not_pad(self) -> None:
        """CTR 是流式模式：长度多奇怪都不需要填充。"""
        payload = b"z" * 37
        cipher = aes_ctr_xor(payload, self.KEY_128, self.IV)
        assert len(cipher) == 37
        assert aes_ctr_xor(cipher, self.KEY_128, self.IV) == payload

    def test_wrong_key_cannot_restore(self) -> None:
        """用错密钥时，要么填充校验失败报错，要么解出乱码 —— 总之拿不回原文。"""
        original = b"important payload!"
        cipher = aes_ecb_encrypt(original, self.KEY_128)
        try:
            wrong = aes_ecb_decrypt(cipher, self.OTHER_KEY)
        except AesError:
            wrong = b""
        assert wrong != original

    def test_invalid_key_length_raises(self) -> None:
        with pytest.raises(AesError, match="密钥长度"):
            aes_ecb_encrypt(b"data", b"short-key")

    def test_invalid_iv_length_raises(self) -> None:
        with pytest.raises(AesError, match="IV 必须是"):
            aes_cbc_encrypt(b"data", self.KEY_128, b"too-short-iv")

    def test_unaligned_plaintext_without_padding_raises(self) -> None:
        with pytest.raises(AesError, match="整数倍"):
            aes_ecb_encrypt(b"abc", self.KEY_128, use_padding=False)

    def test_24_and_32_byte_keys_supported(self) -> None:
        """AES-192 与 AES-256 同样要能用。"""
        for key in (bytes(range(24)), bytes(range(32))):
            cipher = aes_ecb_encrypt(b"payload", key)
            assert aes_ecb_decrypt(cipher, key) == b"payload"


# ---------------------------------------------------------------------------
# crypto.sign —— 待签名串拼装
# ---------------------------------------------------------------------------
class TestSignBaseString:
    """拼装规则必须是可预测的，否则签名永远对不上。"""

    def test_sorted_by_key(self) -> None:
        assert build_sign_base_string({"b": 2, "a": 1}) == "a=1&b=2"

    def test_unsorted_preserves_input_order(self) -> None:
        assert build_sign_base_string({"b": 2, "a": 1}, sort_keys=False) == "b=2&a=1"

    def test_exclude_sign_field(self) -> None:
        """必须能把「即将算出的签名字段」排除掉，否则是死循环。"""
        result = build_sign_base_string({"a": 1, "sign": "xxx"}, exclude={"sign"})
        assert result == "a=1"

    def test_bool_becomes_lowercase(self) -> None:
        """C# / JSON 里布尔是 true/false，不是 Python 的 True/False。"""
        assert build_sign_base_string({"flag": True, "no": False}) == "flag=true&no=false"

    def test_none_becomes_empty_string(self) -> None:
        assert build_sign_base_string({"a": None}) == "a="

    def test_skip_empty(self) -> None:
        result = build_sign_base_string({"a": 1, "b": None, "c": ""}, skip_empty=True)
        assert result == "a=1"

    def test_empty_params_raises(self) -> None:
        with pytest.raises(SignError, match="不能为空"):
            build_sign_base_string({})

    def test_custom_separators(self) -> None:
        result = build_sign_base_string(
            {"a": 1, "b": 2}, pair_separator="|", kv_separator=":"
        )
        assert result == "a:1|b:2"


# ---------------------------------------------------------------------------
# crypto.sign —— 加盐签名与校验
# ---------------------------------------------------------------------------
class TestSignWithSalt:
    def test_matches_manual_concat(self) -> None:
        """签名结果必须等于「拼接后再 MD5」，这是可以独立验证的。"""
        assert sign_with_salt("a=1&b=2", "SALT") == md5_hex("a=1&b=2" + "SALT")

    def test_sha256_algorithm(self) -> None:
        assert len(sign_with_salt("payload", "salt", "sha256")) == 64

    def test_verify_signature_accepts_correct(self) -> None:
        params = {"a": 1, "b": "x"}
        valid_sign = sign_with_salt(build_sign_base_string(params), "SALT")
        assert verify_signature(params, valid_sign, "SALT") is True

    def test_verify_signature_rejects_wrong(self) -> None:
        assert verify_signature({"a": 1}, "deadbeef", "SALT") is False

    def test_verify_signature_ignores_sign_field(self) -> None:
        """参数字典里带着 sign 字段也不应影响结果。"""
        params = {"a": 1}
        valid_sign = sign_with_salt(build_sign_base_string(params), "SALT")
        with_sign = {**params, "sign": valid_sign}
        assert verify_signature(with_sign, valid_sign, "SALT") is True

    def test_sign_request_raises_until_protocol_confirmed(self) -> None:
        """协议未确认前必须报错，绝不静默发出错误请求。"""
        with pytest.raises(SignError, match="签名规则尚未确认"):
            sign_request({"a": 1})


# ---------------------------------------------------------------------------
# crypto.sign —— Token 管理
# ---------------------------------------------------------------------------
class TestTokenStore:
    def test_default_is_invalid(self) -> None:
        assert TokenStore().is_valid() is False

    def test_update_then_valid(self) -> None:
        store = TokenStore()
        store.update(uid="u1", token="t1")
        assert store.is_valid() is True
        assert store.uid == "u1"

    def test_expired_at_deadline(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="t", expires_in=10)
        assert store.expires_at is not None
        assert store.is_expired(now=store.expires_at) is True

    def test_not_expired_before_deadline(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="t", expires_in=10)
        assert store.expires_at is not None
        assert store.is_expired(now=store.expires_at - 1) is False

    def test_no_deadline_never_expires(self) -> None:
        """服务端不下发过期时间时，不主动作废令牌。"""
        assert TokenStore(token="t").is_expired(now=10**12) is False

    def test_clear_resets_everything(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="t", expires_in=100)
        store.clear()
        assert store.token == ""
        assert store.uid == ""
        assert store.expires_at is None
        assert store.is_valid() is False

    def test_as_header(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="abc123")
        assert store.as_header("X-Token") == {"X-Token": "abc123"}

    def test_as_header_requires_token(self) -> None:
        with pytest.raises(SignError, match="没有可用令牌"):
            TokenStore().as_header("X-Token")

    def test_repr_masks_token(self) -> None:
        """repr 绝不能让完整 Token 出现在日志或截图里。"""
        store = TokenStore()
        store.update(uid="u", token="supersecrettoken")
        text = repr(store)
        assert "supersecrettoken" not in text
        assert "supe***" in text


if __name__ == "__main__":  # 支持 `python -m tests.test_crypto`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))



