"""通用响应信封与载荷解析工具。

⚠️ **重要前提，请先读这段**

本游戏协议是 **protobuf**：dump.cs 中 ``com.auer.game.protobuf`` 命名空间下有
1098 个类，全库有 3956 处 ``ProtoMember`` 字段编号。也就是说真实响应是**二进制消息**，
**不一定**存在本文件定义的 JSON 信封结构。

那为什么还要保留这个文件？有两个实际用途：

1. **抓包辅助**：公告、版本检查、SDK 回调这类接口经常仍是 JSON 形式，
   统一解析可以少写大量重复代码；
2. **集中管理「成功判定」**：等确认了真实结构，只需改这一个文件，
   而不必去改 tasks 里的每个任务。

TODO(protocol): ``code`` / ``msg`` / ``data`` 三个字段名**尚未确认**。
    建议先检索：
        bash tools/search_dump.sh 'class .*Res' -C 25 -m 20
    确认后应二选一处理：
      (a) 把这里的字段名改成真实名称；
      (b) 保留 Python 风格字段名，用 ``Field(alias="真实名")`` 建立映射。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from models.base import PayloadError, ProtocolModel


def parse_json_payload(raw: bytes | str) -> Any:
    """把可能是 JSON 的载荷解析成 Python 对象。

    :param raw: 响应体字符串或字节。
    :return: 解析结果（通常是 ``dict`` 或 ``list``）。
    :raises PayloadError: 不是合法 JSON 时；错误信息会附带数据开头片段，
        让你一眼看出「收到的到底是什么」（例如其实是一段 HTML 错误页）。
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError(
            f"响应体不是合法 JSON（{exc.msg}，位置 {exc.pos}）",
            raw_preview=text[:120],
        ) from exc


class ResponseEnvelope(ProtocolModel):
    """通用响应信封（字段名当前为占位，详见模块文档）。"""

    code: int | None = None
    msg: str | None = None
    data: dict[str, Any] | None = None

    #: 假设的成功码。**必须等协议确认**，目前沿用最常见的 0。
    DEFAULT_SUCCESS_CODE: ClassVar[int] = 0

    def is_success(self, success_code: int | None = None) -> bool:
        """判断业务层是否成功。

        :param success_code: 成功码。默认取 :attr:`DEFAULT_SUCCESS_CODE`。
            真实成功码到底是 0 还是 200，必须从 dump.cs 确认，
            因此这里允许逐个调用点覆盖，而不是写死。

        ⚠️ 当响应里**没有** ``code`` 字段（值为 ``None``）时返回 ``False``，
        而不是「当作成功」。宁可让你多看一眼日志，也不要默默把失败当成功 ——
        后者会让你在「任务明明没成功却显示成功」的坑里浪费半天。
        """
        if self.code is None:
            return False
        expected = self.DEFAULT_SUCCESS_CODE if success_code is None else success_code
        return self.code == expected

    def message(self) -> str:
        """返回可读的错误描述（无描述时给出占位文本，避免日志出现 ``None``）。"""
        return self.msg or "<服务端未提供描述>"
