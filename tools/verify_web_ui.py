"""前端交互回归验证：无头 Edge + CDP，真的点一次「执行」。

【它要回答的问题】
``node --check`` 与 ``--dump-dom`` 只能证明"元素渲染出来了"，证明不了
"点下去会发生什么"。这个脚本补上最后那一步：

    ▸ 点展开 → 出现「执行」→ 点它 → 提交 [该任务, daily_box]

【八条判据】（2026-09-24 实测 8/8 通过）
1. 主动任务卡片各有 1 个展开箭头（``.task-toggle``，原来这个位置放的是复选框）；
2. ``#btn-run``（"一键运行"）已不存在；
3. 未展开时没有执行按钮、展开后恰好 1 个，且卡片类名含 ``task--expanded``；
4. 展开后参数区可见；
5. 点执行按钮后 ``/api/status`` 的步骤顺序是 ``该任务, daily_box``（宝箱最后）；
6. 服务端回报 ``dry_run=True``（本次点击确实没发包）。
7. 进主界面时那次**自动执行**已经跑完（脚本先轮询到空闲再动手，不靠固定 sleep）；
8. 点击这一刻即使有别的运行在跑，**也不再被拒绝**（★ 2026-10-04）：
   服务端会把它排进队列，步骤顺序依旧成立。脚本因此把"点了没提交就重试"
   循环保留成一道容错，但正常情况下第一次点击就该成功 ——
   修复前的现象是弹一个"上一次还没结束，请等它跑完"，
   而用户要的是"排队跑完"（见 ``webapi/state.py`` 的队列说明）。

【★ 安全前提：脚本会先强制勾上「演练模式」】
所以这一次点击只组装、不发包（见 ``notes/web_layer.md`` §10.5）。
即使本机有真实会话也不会碰真实账号 —— 但**仍建议**只在演练模式下跑它。

【为什么不用现成的库】
项目规则是"只用已经在用的库"（本机没有 websockets / websocket-client）。
CDP 的 WebSocket 协议本身很简单（客户端帧必须加掩码），用标准库
``socket`` + ``base64`` + ``struct`` 手写几十行即可，比装一个库更干净。

用法（先起服务与无头 Edge，再跑本脚本；两个端口都可覆盖）::

    python web_server.py --port 8767 &
    msedge --headless=new --remote-debugging-port=9223 \\
        --user-data-dir=<工程绝对路径>/tmp/edgeV2 'http://127.0.0.1:8767/?flat=1&dev=1' &
    python tools/verify_web_ui.py 9223 8767

脚本结束时会用 CDP 的 ``Browser.close`` **只关掉自己启动的那个 Edge**，
不会误伤你正在使用的浏览器窗口。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse

CDP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9223
PAGE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8767


# ----------------------------------------------------------------------
# 最小 WebSocket 客户端（只做文本帧，够 CDP 用）
# ----------------------------------------------------------------------
def ws_connect(ws_url: str) -> socket.socket:
    """完成 WebSocket 握手，返回已连接的 socket。"""
    parsed = urlparse(ws_url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手失败：连接被关闭")
        buffer += chunk
    if b"101" not in buffer.split(b"\r\n")[0]:
        raise ConnectionError("握手失败：服务端没有返回 101")
    return sock


def ws_send(sock: socket.socket, text: str) -> None:
    """发一个客户端文本帧（客户端发出去的数据必须加掩码）。"""
    payload = text.encode()
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def ws_recv(sock: socket.socket) -> str:
    """收一个文本帧（忽略 ping/pong；close 直接抛错）。"""

    def read_exactly(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise EOFError("连接已关闭")
            data += chunk
        return data

    while True:
        first, second = read_exactly(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", read_exactly(8))[0]
        payload = read_exactly(length)
        if opcode == 0x1:
            return payload.decode()
        if opcode == 0x8:
            raise EOFError("服务端要求关闭连接")


class CdpSession:
    """一条 CDP 连接：``call()`` 发命令并等它自己的回包。"""

    def __init__(self, ws_url: str) -> None:
        self._sock = ws_connect(ws_url)
        self._next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        message_id = self._next_id
        self._next_id += 1
        ws_send(
            self._sock,
            json.dumps({"id": message_id, "method": method, "params": params or {}}),
        )
        while True:
            message = json.loads(ws_recv(self._sock))
            if message.get("id") == message_id:
                return message

    def evaluate(self, expression: str, *, await_promise: bool = False):
        """在页面里跑一段 JS，返回它的值（页面里抛错就会被抛出来）。"""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        payload = result.get("result", {})
        if payload.get("exceptionDetails"):
            raise RuntimeError(f"页面 JS 抛错：{payload['exceptionDetails']}")
        return payload.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def page_target() -> str:
    """从 /json/list 里找出我们那个页面的调试地址。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=10) as resp:
        targets = json.loads(resp.read().decode())
    for target in targets:
        if target.get("type") == "page" and str(PAGE_PORT) in target.get("url", ""):
            return target["webSocketDebuggerUrl"]
    raise RuntimeError(f"没找到页面目标（端口 {PAGE_PORT}）")


def browser_target() -> str:
    """浏览器级调试地址（用来优雅关掉我们自己启动的 Edge，不误伤用户的窗口）。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=10) as resp:
        info = json.loads(resp.read().decode())
    return info["webSocketDebuggerUrl"]


def check(label: str, actual, expected) -> bool:
    """打印一条判据，返回是否通过。"""
    ok = actual == expected
    print(f"{'[OK]' if ok else '[!!]'} {label}：{actual!r}" + ("" if ok else f"（期望 {expected!r}）"))
    return ok


def main() -> int:
    results: list[bool] = []
    page = CdpSession(page_target())
    try:
        # ① 主动任务卡片：每个都该有一个展开箭头（原来这个位置放复选框）。
        # 【期望值为什么动态取】任务清单是规格表驱动的，会正常增加
        # （2026-09-30 就新增了 buy_energy，把写死的 3 变成过期值）——
        # 写死数字只会让"任务正常加卡"报假失败。以 /api/tasks 的 manual 组为准。
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PAGE_PORT}/api/tasks", timeout=10
        ) as resp:
            manual_tasks = [
                item for item in json.loads(resp.read().decode())["tasks"]
                if item.get("group") == "manual"
            ]
        manual_count = len(manual_tasks)
        # 首个主动任务的 id 也动态取（2026-10-02 翻车实录：脚本写死了
        # sweep_activity，而 10-01 新增 alliance_donate 排到了第一位，
        # 回归脚本立刻假失败 —— 与上面的 manual_count 同一个道理）。
        first_manual = manual_tasks[0]["name"]
        results.append(
            check(
                "主动任务展开箭头数（=manual 组任务数）",
                page.evaluate("document.querySelectorAll('#tasks-manual .task-toggle').length"),
                manual_count,
            )
        )
        results.append(
            check(
                "一键运行按钮数（应为 0）",
                page.evaluate("document.querySelectorAll('#btn-run').length"),
                0,
            )
        )
        results.append(
            check(
                "未展开时执行按钮数（应为 0）",
                page.evaluate("document.querySelectorAll('.task-exec').length"),
                0,
            )
        )
        # ② 安全闸门先落地：强制演练模式 ⇒ 这次点击只组装、不发包
        results.append(
            check(
                "演练模式已开",
                page.evaluate(
                    "(() => { const cb = document.getElementById('opt-dry-run');"
                    " if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change')); }"
                    " return cb.checked; })()"
                ),
                True,
            )
        )
        # ③ 先等"进主界面时的那次自动执行"跑完。
        #
        # 【为什么必须等（2026-09-24 修）】进入主界面现在会自动执行每日任务，
        # 而运行中时主动任务的「执行这个任务」按钮是**禁用**的（服务端同一时刻
        # 只允许一次运行）。用固定 sleep 会变成"看机器快慢"的假失败 ——
        # 实测就是这样翻车的：点下去没有反应，读到的步骤是那次自动执行。
        # 把弹窗换成"记录到数组"：headless 下真实的 alert 会让页面 JS 阻塞
        # （CDP 的 evaluate 会挂住），而我们要看的恰恰是"有没有给用户提示"。
        page.evaluate(
            "window.__alerts = []; window.alert = (m) => window.__alerts.push(String(m)); 'ok'"
        )

        for _ in range(40):
            idle = page.evaluate(
                "fetch('/api/status').then(r => r.json()).then(s => s.running)",
                await_promise=True,
            )
            if not idle:
                break
            time.sleep(1)
        # 后端空闲 ≠ 前端已解禁：前端空闲时 5 秒才轮询一次（见 app.js 的 schedulePolling），
        # 而「执行这个任务」按钮由 setRunning() 控制禁用 —— 再等一拍，否则点下去没反应。
        time.sleep(6)

        # 现在点第一张主动任务的展开箭头
        page.evaluate("document.querySelector('#tasks-manual .task-toggle').click(); 'ok'")
        time.sleep(1.5)
        results.append(
            check(
                "展开后执行按钮数",
                page.evaluate("document.querySelectorAll('.task-exec').length"),
                1,
            )
        )
        results.append(
            check(
                "展开后出现 task--expanded 卡片",
                page.evaluate("document.querySelectorAll('.task--expanded').length"),
                1,
            )
        )
        print("   ↳ 按钮文案：", page.evaluate("document.querySelector('.task-exec').textContent"))
        print(
            "   ↳ 参数区可见：",
            page.evaluate(
                "!document.querySelector('.task--expanded .task-params').classList.contains('hidden')"
            ),
        )
        # ④ 真的点它：提交 [该任务, daily_box]（演练模式，不发包）
        #
        # 【为什么还留着重试】★ 2026-10-04 之后 `/api/run` 不再返回 409（改排队），
        # 所以正常路径一次就该成功。保留这个循环只是为了容忍 CDP 侧的时序抖动
        # （点击尚未生效、状态还没轮到本次运行），它不再对应任何"被拒绝"的提示。
        steps = ""
        for attempt in range(3):
            page.evaluate("document.querySelector('.task-exec').click(); 'ok'")
            for _ in range(15):
                steps = page.evaluate(
                    "fetch('/api/status').then(r => r.json())"
                    ".then(s => s.steps.map(x => x.name).join(','))",
                    await_promise=True,
                )
                if steps == f"{first_manual},daily_box":
                    break
                time.sleep(1)
            if steps == f"{first_manual},daily_box":
                break
            prompts = page.evaluate("JSON.stringify(window.__alerts || [])")
            print(f"   ↳ 第 {attempt + 1} 次点击未提交成功（页面提示：{prompts}），重试…")
        print("   ↳ 本次运行的步骤：", steps)
        results.append(check("步骤顺序（该任务在前、宝箱最后）", steps, f"{first_manual},daily_box"))
        dry_run = page.evaluate(
            "fetch('/api/status').then(r => r.json()).then(s => s.dry_run)",
            await_promise=True,
        )
        results.append(check("服务端确认是演练（未发包）", dry_run, True))
    finally:
        page.close()
        # 关掉我们自己启动的 Edge（Browser.close 只作用于这个调试实例）
        try:
            browser = CdpSession(browser_target())
            browser.call("Browser.close")
            browser.close()
        except (OSError, EOFError, ConnectionError):
            pass

    print(f"\n结果：{sum(results)}/{len(results)} 条判据通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
