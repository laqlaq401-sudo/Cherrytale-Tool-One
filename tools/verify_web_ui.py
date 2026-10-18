"""前端交互回归验证：无头 Edge + CDP，真的点一次「执行」。

【它要回答的问题】
``node --check`` 与 ``--dump-dom`` 只能证明"元素渲染出来了"，证明不了
"点下去会发生什么"。这个脚本补上最后那一步：

    ▸ 点展开 → 出现「执行」→ 点它 → 提交 [该任务, daily_box]

【判据】
单卡执行（2026-09-24 起）：
1. 主动任务卡片各有 1 个展开箭头（``.task-toggle``）；
2. ``#btn-run``（"一键运行"）已不存在；
3. 未展开时没有执行按钮、展开后恰好 1 个，且卡片类名含 ``task--expanded``；
4. 展开后参数区可见；
5. 点执行按钮后 ``/api/status`` 的步骤顺序是 ``该任务, daily_box``（宝箱最后）；
6. 服务端回报 ``dry_run=True``（本次点击确实没发包）；
7. 点击这一刻即使有别的运行在跑，**也不再被拒绝**（★ 2026-10-04）：
   服务端会把它排进队列，步骤顺序依旧成立。脚本因此把"点了没提交就重试"
   循环保留成一道容错，但正常情况下第一次点击就该成功。

一键执行（★ 2026-10-16）：
8. 参与一键执行的卡片（``batch_eligible``）各有 **1 个**勾选框；
   ``buy_energy`` / ``push_main_stage`` 是 **0 个**（它们不参与，也不该有假控件）；
9. ``#btn-run-manual`` 存在，且**未勾选时禁用**；
10. 勾两个任务后点它 → 步骤顺序 = ``任务A, daily_box, 任务B, daily_box``
    （宴席若"此刻能吃"会排在更前面 —— 所以这里刻意挑两个非宴席任务，
    否则判据会随宴席的窗口状态时灵时不灵）；
11. 点「停止执行」后，这一批剩下的步骤状态是 ``skipped``（**不是** ``fail``：
    它不是做错了，而是压根没做）。
    ⚠️ 必须先**连点把队列排起来**再点停止 —— 单批 dry-run 只要 0.79 秒就跑完，
    等按钮转成「停止执行」时它早结束了，"没东西可跳"必然假红。

「展开不闪」与状态真源（★ 2026-10-16，用户报的"按钮闪烁"修复）：
12. **展开另一张卡时，已展开卡片的执行按钮节点不被重建**，配色与文案逐字节不变
    —— 这是那个闪烁 bug 的照妖镜（旧实现走整组重建，节点必然被换掉）。
    整段判定塞在**一个 JS 回合**里跑完，所以不受"这次运行要跑多久"影响。
13. 折叠/再展开**不重建箭头节点**（节点同一性）、**不把滚动位置弹回顶部**。
    焦点探针在同一回合内 ``focus()`` 箭头自身再折叠/展开：节点被换掉时
    ``activeElement`` 会掉到 body。⚠️ 无头 Edge 里 ``focus()`` 有时拿不到焦点
    （``canFocus=false``，2026-10-16 实测），那时这条由"节点未被重建"承担，
    不谎报通过也不假红。
14. 界面上的「执行中 / 排队中」（``.task-stop`` 类）与 ``/api/status`` 的
    ``steps``（status 为 pending/running）**完全一致**，且**服务端确实有活动步骤**
    （``active > 0``，否则"两边都是空集"会假通过）；排起队列后验一次，
    刷新页面之后再验一次（治"状态记在前端内存、刷新就丢"的老毛病）。
    ⚠️ 同时断言「一键执行」按钮在有批次时转成「停止执行」（双态按钮）。

【★ 为什么到处都是"连点排队"】
dry-run 一步只要 ~0.2 秒（实测 4 步整批 **0.79 秒**）。任何"需要有一批在跑"的
判据，若只提交**一批**，都会在可观察之前就结束。所以需要活动态的判据一律
先 ``queue_streak()`` 连点几次：排队批次的步骤一直是 ``pending``、且每批同名，
于是"哪些任务处于活动态"这个**名字集合**在若干秒内恒定 —— 前端 1.2s 的轮询
能稳定对齐，竞态被消掉，而不是靠"多等一会儿"。

【脚本开头的环境摆正（★ 2026-10-16 起）】
清零卡片的展开状态（服务端持久化，见 ``services/ui_prefs.py``）后**自己导航到
``CANONICAL_URL``**：不清就会带着上次跑完的展开卡片进判据 ——"未展开时执行按钮数
（应为 0）"会直接误红。导航带 ``&api=`` 的原因见 ``CANONICAL_URL`` 的注释
（``getApiOrigin()`` 会把非 8765 端口悄悄指回 8765）。

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
        --user-data-dir=<工程绝对路径>/tmp/edgeV2 'about:blank' &
    python tools/verify_web_ui.py 9223 8767

⚠️ Edge 开在哪个地址**无所谓**（``about:blank`` 也行）：脚本开头会自己
``Page.navigate`` 到 ``CANONICAL_URL``，并且那个地址里**必须**带
``&api=http://127.0.0.1:<PAGE_PORT>``。原因见 ``CANONICAL_URL`` 的注释 ——
``getApiOrigin()`` 会把非 8765 端口的页面悄悄指回 8765，导致页面"看着正常、
接口手测 200，就是任务列表空的"（2026-10-16 实际踩过，排查了半小时）。

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
    """从 /json/list 里找出我们那个页面的调试地址。

    ★ 2026-10-16：**不再要求目标 URL 里带端口**。
    脚本现在会自己 ``Page.navigate`` 到规范地址（``CANONICAL_URL``），所以
    Edge 是被 ``about:blank`` 还是被别的地址拉起来的都无所谓 —— 优先挑带端口的，
    挑不到就退而取第一个 page 目标。
    """
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=10) as resp:
        targets = json.loads(resp.read().decode())
    pages = [t for t in targets if t.get("type") == "page"]
    for target in pages:
        if str(PAGE_PORT) in target.get("url", ""):
            return target["webSocketDebuggerUrl"]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    raise RuntimeError(f"没找到任何页面目标（CDP 端口 {CDP_PORT}）")


#: 脚本自己导航到的规范页面地址。
#:
#: ★ 2026-10-16：**``&api=`` 这个参数不能省**，否则页面一个请求都发不对。
#: ``web/assets/common.js::getApiOrigin()`` 的规则是："端口不是 8765 / 8000
#: （8000 是安卓包内服务器）时，一律把 API 指回 ``<host>:8765``"。校验脚本
#: 为了不撞用户正在用的 8765，惯例跑在 8767 —— 于是页面会把 ``/api/health``
#: 发到**没人监听的 8765**：``conn--bad``（"服务不可用"）→ ``boot()`` 抛错被
#: ``catch`` 吞掉 → ``loadTasks()`` 压根没跑 → ``#tasks-manual`` 是个**空 div**。
#: 症状极具误导性：页面 ``readyState=complete``、零 JS 异常、接口手测 200，
#: 只有"任务列表是空的"。实测踩过（2026-10-16，排查花了半小时）。
#: 修法是显式声明 API 地址，让页面与校验脚本永远指向同一份服务。
CANONICAL_URL = (
    f"http://127.0.0.1:{PAGE_PORT}/?flat=1&dev=1&api=http://127.0.0.1:{PAGE_PORT}"
)


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


#: ★ 2026-10-16：首屏任务清单"装配完成"的探针。
#:
#: 【为什么必须等，不能 sleep 固定几秒】``Page.reload``（以及 Edge 刚打开那一刻）
#: 的 CDP 回包只代表"导航命令被受理"，页面后面还要走
#: ``boot → GET /api/tasks → renderTaskGroups``。这段窗口里 ``#tasks-manual``
#: 底下**一个节点都没有**，于是：
#:   ▸ 按 class 计数的判据会**假通过**（0 == 0，比如"未展开时执行按钮数应为 0"）；
#:   ▸ 取单节点的 ``document.querySelector('#tasks-manual .task-toggle')`` 是 ``null``
#:     → 点它直接抛 ``Cannot read properties of null``。
#: 实测翻车就是这两条一起出现：预检刷新后只 ``sleep(5)`` 就断言，读到
#: "展开箭头 0 个（期望 8）"，紧接着点箭头炸掉。机器快慢会决定 5 秒够不够 ——
#: 那就是把"看机器快慢"的假失败请回来了（同样的坑在下面等自动执行时已经踩过一次）。
#:
#: 判据取 ``#tasks-manual .task`` 的节点数：卡片的**根节点**先于内部控件出现，
#: 但它 > 0 时 ``renderTaskGroups`` 已经跑完，内部控件必然都在。
RENDER_READY_JS = "document.querySelectorAll('#tasks-manual .task').length"


def wait_for_render(page: CdpSession, *, timeout: float = 40.0, interval: float = 0.5) -> int:
    """等首屏任务卡真的装配出来，返回卡片数；超时**直接抛错**（不静默往下走）。

    【为什么超时要抛错而不是返回 0】返回 0 会把"页面根本没起来"伪装成
    "界面确实没有卡片"，后面的判据再逐条假通过 —— 那比直接失败更坏。
    """
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            count = page.evaluate(RENDER_READY_JS) or 0
        except RuntimeError as exc:  # 导航中执行上下文还没建好：重试而不是放弃
            last_error = str(exc)
            count = 0
        if count:
            return int(count)
        time.sleep(interval)
    raise RuntimeError(
        f"等首屏任务清单装配超时（{timeout:.0f}s 内 #tasks-manual .task 始终为 0）"
        + (f"；最后一次 evaluate 报错：{last_error}" if last_error else "")
    )


def reset_expanded_prefs() -> None:
    """把服务端持久化的"卡片展开状态"清零（供脚本开头摆正环境用）。

    【为什么走 Python 侧 HTTP 而不是页面里的 ``fetch``】页面刚导航完时它的
    JS 执行上下文可能还没建立，``page.evaluate`` 会拿不到返回值；而且走 HTTP
    能顺手校验状态码 —— 页面里的 ``fetch().then()`` 无论 4xx/5xx 都会"成功"，
    清零失败会被静默吞掉，然后下面的判据莫名其妙地红。
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{PAGE_PORT}/api/ui_prefs",
        data=json.dumps({"expanded": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"清零卡片展开状态失败：HTTP {resp.status}")


#: ★ 2026-10-16：界面上的「执行中 / 排队中」必须与 ``/api/status`` 完全一致。
#:
#: 【判据】每个执行按钮的 ``task-stop`` 类 ⇔ 服务端 ``steps`` 里存在同名且
#: ``status`` 为 ``pending`` / ``running`` 的步骤。返回**不一致的按钮清单**
#: （``名字:多`` = 界面说能停、服务端说没有；``名字:少`` = 服务端说在跑、
#: 界面还显示「执行」）。空串 = 完全一致。
#:
#: 【它能抓到什么】① 之前那份"前端自己记 stopTasks、刷新就丢"的老毛病
#: （刷新后服务端说在跑、界面说没在跑）；② 渲染路径没把状态施加上的回归。
#:
#: 【★ 2026-10-16 返回 ``{bad, active}`` 而不是一个字符串】
#: ``active`` = 服务端此刻 pending/running 的**任务名个数**。没有它这条判据会
#: **假通过**：服务端空闲、界面也没有任何 ``.task-stop`` 时两边都是空集，
#: ``bad`` 当然为空 —— 于是"什么都没在跑"被当成"两边一致"。
#: 调用方必须同时要求 ``active > 0``，才算真的比过一次。
DOM_SERVER_SYNC_JS = (
    "fetch('/api/status').then(r => r.json()).then((s) => {"
    " const active = new Set((s.steps || [])"
    "   .filter(x => x.status === 'pending' || x.status === 'running')"
    "   .map(x => x.name));"
    " const bad = [];"
    " document.querySelectorAll('.task-exec, .task-auto-exec').forEach((b) => {"
    "   const name = b.getAttribute('data-task');"
    "   if (!name) return;"
    "   const stopping = b.classList.contains('task-stop');"
    "   if (stopping !== active.has(name)) bad.push(name + (stopping ? ':多' : ':少'));"
    " });"
    " return { bad: bad.join(','), active: active.size };"
    "})"
)


#: 取"**最新那一批**"的步骤名（``,`` 拼接）。
#:
#: 【为什么不能直接 ``s.steps.map(...).join(',')``】★ 2026-10-16 查清：
#: ``webapi/state.py::status()`` 的 ``steps`` 是**扁平化**的 —— 它把
#: ``_recent[-10:]``（**已经跑完的历史批次**）+ 正在跑的 + 排队中的拼成一条列表。
#: 而本脚本早先的断言拿**整条串**做全等比较（期望 ``sweep_activity,daily_box``），
#: 于是只要服务端这一进程里跑过任何别的东西，判据就永远不可能成立。
#:
#: 【什么时候必然跑过别的】校验脚本每次都是"全新服务进程"，而它开页时前端会
#: 触发一次「启动」自动任务；``.daily_state.json`` 不存在时 ``auto_done`` 恒为空，
#: 这一批**每次都会重跑**并落进 ``_recent`` —— 于是步骤串前面永远挂着十几个
#: 自动任务的步骤。断言因此常红，且重试循环会把同一批**重复提交 3 次**
#: （实测：步骤列表里出现 3 份 ``top_pvp_battle,daily_box``）。
#: 已用改动前的代码跑过基线对照，确认这三条**改前就是红的**，不是本次改动引入。
#:
#: 【修法】步骤项自带 ``run_id``（``state.py::StepState.to_dict``），按**最后出现的
#: 那个 run_id** 分组即可拿到"刚提交的这一批"。步骤顺序是"历史 → 当前 → 排队"，
#: 所以最后一段正是最新提交的那一批。若步骤项没有 ``run_id``（老服务端），
#: ``lastId`` 为 ``undefined``、全项同名 → 行为退化成"取全部"，与旧逻辑一致。
LATEST_RUN_STEPS_JS = (
    "fetch('/api/status').then(r => r.json()).then((s) => {"
    " const st = s.steps || [];"
    " if (!st.length) return '';"
    " const lastId = st[st.length - 1].run_id;"
    " return st.filter(x => x.run_id === lastId).map(x => x.name).join(',');"
    "})"
)


#: 连点「一键执行」的次数 —— 用来制造一个**稳定的"活动中"窗口**。
#:
#: 【为什么必须这么做】★ 2026-10-16 实测：dry-run 下一步只要 ~0.2 秒
#: （2 个任务 + 2 次宝箱 = 4 步，**整批 0.79 秒就跑完**）。于是"单批正在跑"
#: 这个窗口短到前端 1.2 秒的轮询根本抓不住，所有"需要有一批在跑"的判据都会
#: 退化成空转：界面没有 ``.task-stop``、服务端也没有 pending/running，两边都是
#: 空集，怎么比都"一致"；点「停止执行」更是无从谈起 —— 早跑完了，没东西可跳。
#: 这也是那三条老判据（步骤顺序 / skipped）此前一直红的原因之一。
#:
#: 【为什么连点会排队、而不是互相停掉】按钮的"执行/停止"双态判定是
#: ``state.running && state.batchRunId``（见 app.js::renderManualBatchStatus），
#: 而 ``state.running`` 是**轮询得来的**。连点间隔远小于一个轮询周期，
#: 每次点击都走"提交"分支 ⇒ 队列里排起 N 批。
#:
#: 【为什么这样就没有竞态了】排队批次的步骤**一直是 pending**，且每批内容相同：
#: 前一批跑完了，后一批里同名步骤仍是 pending。于是"哪些任务处于活动态"这个
#: **名字集合**在若干秒内恒定 —— 前端 1.2 秒的轮询能稳定对齐，不存在"刚好在
#: 两次轮询之间跑完"的窗口。这不是"多等一会儿等它变绿"，是把竞态消掉了。
QUEUE_STREAK = 6


def queue_streak(page: CdpSession, times: int = QUEUE_STREAK) -> None:
    """连点「一键执行」``times`` 次，把队列排起来（dry-run，不发包）。"""
    for _ in range(times):
        page.evaluate("document.getElementById('btn-run-manual').click(); 'ok'")


def wait_batch_stopping(page: CdpSession, *, timeout: float = 8.0) -> bool:
    """等「一键执行」按钮转成「停止执行」（前端轮询跟上要一个周期）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate(
            "(() => { const b = document.getElementById('btn-run-manual');"
            " return Boolean(b && !b.disabled"
            " && b.classList.contains('task-stop')"
            " && b.textContent.trim() === '停止执行'); })()"
        ):
            return True
        time.sleep(0.3)
    return False


def sample_sync(page: CdpSession, *, rounds: int = 16, interval: float = 0.5) -> dict:
    """反复采样"界面 vs 服务端"，返回收敛后的那一份 ``{bad, active}``。

    停止条件有两个、**缺一不可**：``bad`` 为空 **且** 服务端确实有活动步骤
    （``active > 0``）。只看 ``bad`` 会拿到"两边都是空集"的假通过 ——
    正是修这个脚本时踩到的坑（详见 ``DOM_SERVER_SYNC_JS`` 的说明）。
    """
    snapshot = {"bad": "<未采样>", "active": 0}
    for _ in range(rounds):
        snapshot = page.evaluate(DOM_SERVER_SYNC_JS, await_promise=True)
        if snapshot["bad"] == "" and snapshot["active"] > 0:
            return snapshot
        time.sleep(interval)
    return snapshot


def main() -> int:
    results: list[bool] = []
    page = CdpSession(page_target())
    try:
        # ⓪ ★ 2026-10-16：先把"卡片展开状态"清零并刷新页面。
        #
        # 【为什么必须做（不做就会误红）】展开状态从 2026-10-16 起是**服务端
        # 持久化**的（services/ui_prefs.py）：上一次跑完留下的展开卡片会让
        # 下面"未展开时执行按钮数（应为 0）"直接失败 —— 等于给脚本引入了
        # 环境依赖。刷新页面是为了让前端按服务端的空集合重新装配
        # （关掉 DOM 是不够的：``state.expanded`` 也要跟着空，否则下面点第一张
        # 卡的箭头会变成"折叠"而不是"展开"）。
        # 做法与下面"强制打开演练模式"同类：先摆正环境，再验行为。
        #
        # ⚠️ 每一步都要**等到位再往下走**（``wait_for_render``），不能用固定 sleep：
        # 装配没完成时页面里根本没有 ``#tasks-manual .task``，这时断言
        # "未展开时执行按钮数应为 0"会假通过，紧接着点展开箭头会因取到 ``null``
        # 而抛 ``Cannot read properties of null`` —— 实测就是这么翻车的。
        page.call("Page.enable")
        reset_expanded_prefs()  # ① 清零（HTTP 直发，顺带校验状态码）
        # ② 自己导航到规范地址：带上 ``&api=`` 把页面钉在本脚本这份服务上。
        # 顺手把"Edge 是被哪个 URL 拉起来的"这个外部变量从脚本里去掉。
        page.call("Page.navigate", {"url": CANONICAL_URL})
        cards_at_boot = wait_for_render(page)  # ③ 等首屏真的装配出来
        print(f"   ↳ 首屏卡片数：{cards_at_boot}")
        print("   ↳ 页面 API 基址：", page.evaluate("location.origin"))

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

        # ③.5 ★ 2026-10-16：**展开另一张卡不能动到已展开卡片的按钮**。
        #
        # 【为什么必须有这条（它就是用户报的那个 bug）】展开以前走
        # ``renderTaskGroups()``（整组重建），于是列表里**所有**卡片的执行按钮
        # 都被换成新节点、状态退回常态「执行」，要等下一次轮询（运行中 1.2s /
        # 空闲 5s）才变回来 —— 用户看到的就是"正在执行/排队中的按钮闪一下
        # 变成没执行"。现在展开只动那一张卡片的 DOM。
        #
        # 【为什么整段塞进**一个** JS 回合】节点同一性只能在一个回合里比
        # （跨 evaluate 拿不到节点引用），而这恰好也把时间窗压成零：
        # 判定不受"这次运行要跑多久"影响，不会时灵时不灵。
        flicker = page.evaluate(
            "(() => {"
            " const cardA = document.querySelector('#tasks-manual .task--expanded');"
            " if (!cardA) return { error: 'no-expanded-card' };"
            " const nameA = cardA.dataset.task;"
            " const btnBefore = cardA.querySelector('.task-exec');"
            " if (!btnBefore) return { error: 'no-exec-button' };"
            " const clsBefore = btnBefore.className;"
            " const textBefore = btnBefore.textContent;"
            " const other = [...document.querySelectorAll('#tasks-manual .task')]"
            "   .find((c) => {"
            "     if (c.dataset.task === nameA) return false;"
            "     const t = c.querySelector('.task-toggle');"
            "     return Boolean(t && !t.disabled);"
            "   });"
            " if (!other) return { error: 'no-second-card' };"
            " const otherToggle = other.querySelector('.task-toggle');"
            " otherToggle.click();"
            " const cardAfter = document.querySelector("
            "   '#tasks-manual .task[data-task=\"' + nameA + '\"]');"
            " const btnAfter = cardAfter && cardAfter.querySelector('.task-exec');"
            " const seen = {"
            "   sameNode: btnBefore === btnAfter,"
            "   clsBefore, textBefore,"
            "   clsAfter: btnAfter ? btnAfter.className : null,"
            "   textAfter: btnAfter ? btnAfter.textContent : null,"
            " };"
            " otherToggle.click();"  # 还原：别影响后面的判据
            " return seen;"
            "})()"
        )
        print("   ↳ 展开另一张卡后的对比：", flicker)
        results.append(
            check(
                "展开另一张卡：已展开卡片的按钮节点未被重建",
                flicker.get("sameNode"),
                True,
            )
        )
        results.append(
            check(
                "展开另一张卡：已展开卡片的按钮配色/文案不变",
                (flicker.get("clsAfter"), flicker.get("textAfter")),
                (flicker.get("clsBefore"), flicker.get("textBefore")),
            )
        )

        # ③.6 ★ 2026-10-16：展开/折叠不打断输入焦点、不把滚动弹回顶部。
        # 判据用"切换箭头的节点自身"做焦点探针：同一个 JS 回合里 focus 它、
        # 折叠、再展开 —— 若卡片被重建，这个节点会被换掉，activeElement
        # 会掉到 body 上（老实现就是这样）。
        rebound = page.evaluate(
            "(() => {"
            " const card = document.querySelector('#tasks-manual .task--expanded');"
            " const toggle = card && card.querySelector('.task-toggle');"
            " if (!toggle) return { error: 'no-expanded-card' };"
            " const name = card.dataset.task;"
            " toggle.focus();"
            " const canFocus = document.activeElement === toggle;"
            " const y = window.scrollY;"
            " toggle.click();"
            " toggle.click();"
            " const cardAfter = document.querySelector("
            "   '#tasks-manual .task[data-task=\"' + name + '\"]');"
            " const toggleAfter = cardAfter && cardAfter.querySelector('.task-toggle');"
            " return {"
            "   toggleDisabled: toggle.disabled,"
            "   canFocus,"
            "   sameNode: toggleAfter === toggle,"
            "   focusKept: document.activeElement === toggle,"
            "   scrollSame: window.scrollY === y,"
            " };"
            "})()"
        )
        print("   ↳ 折叠/再展开前后的焦点与滚动：", rebound)
        # 硬判据是**节点同一性**：折叠只该切类名/移除展开体，箭头本身留在原位。
        # 老实现整组重建 → 这个节点被换掉。
        results.append(
            check("折叠/再展开：箭头节点未被重建", rebound.get("sameNode"), True)
        )
        # 焦点这条：无头 Edge 里 ``focus()`` 有时拿不到焦点（``canFocus`` 为假，
        # 实测 2026-10-16 就是如此），那时不谎报通过、也不假红 —— 该有的保障由
        # 上面那条"节点未被重建"承担（节点没换，焦点自然不会被带走）。
        # 真正能拿到焦点时必须守住：焦点仍在同一个箭头上。
        results.append(
            check(
                "折叠/再展开：焦点没被换掉的节点带走",
                (not rebound.get("canFocus")) or rebound.get("focusKept"),
                True,
            )
        )
        results.append(
            check("折叠/再展开：滚动位置不变", rebound.get("scrollSame"), True)
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
                steps = page.evaluate(LATEST_RUN_STEPS_JS, await_promise=True)
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

        # --------------------------------------------------------------
        # ⑤ 一键执行（★ 2026-10-16）
        # --------------------------------------------------------------
        batch_order = page.evaluate(
            "fetch('/api/tasks').then(r => r.json()).then(p => (p.batch_tasks || []).join(','))",
            await_promise=True,
        )
        print("   ↳ 一键执行成员顺序：", batch_order)
        eligible = [item["name"] for item in manual_tasks if item.get("batch_eligible")]
        excluded = [
            item["name"] for item in manual_tasks if not item.get("batch_eligible")
        ]
        # 勾选框数 = 参与一键执行的任务数（卡片上的 data-task 是 2026-10-16 加的，
        # 让脚本能点"指定任务"的控件 —— 否则只能按位置数，结果随机）
        page.evaluate(
            "window.__batchPickSel = (n) => '.task[data-task=\"' + n + '\"] "
            ".task-actions--pick input[type=checkbox]'; 'ok'"
        )
        results.append(
            check(
                "参与一键执行的卡片各有 1 个勾选框",
                page.evaluate(
                    "(function(){ const names = "
                    + json.dumps(eligible)
                    + "; return names.filter((n) => document.querySelectorAll("
                    "window.__batchPickSel(n)).length === 1).length; })()"
                ),
                len(eligible),
            )
        )
        results.append(
            check(
                "不参与的卡片没有勾选框（应为 0 个）",
                page.evaluate(
                    "(function(){ const names = "
                    + json.dumps(excluded)
                    + "; return names.reduce((sum, n) => sum + document.querySelectorAll("
                    "window.__batchPickSel(n)).length, 0); })()"
                ),
                0,
            )
        )
        # 【为什么先点一次「全不选」】勾选状态存在**服务端**（selection_state），
        # 与浏览器 profile 无关 —— 用户上次勾过几个，这次一进来就是勾着的。
        # 不先清零，下面"未勾选时按钮禁用"这条判据会随上次的选择时灵时不灵。
        checked_before = page.evaluate(
            "document.querySelectorAll('#tasks-manual "
            ".task-actions--pick input[type=checkbox]:checked').length"
        )
        if checked_before:
            print(f"   ↳ 已有 {checked_before} 个勾选框是选中的，先点「全不选」清零")
        page.evaluate("document.getElementById('btn-select-none').click(); 'ok'")
        time.sleep(0.4)
        results.append(
            check(
                "「全不选」清空全部勾选框",
                page.evaluate(
                    "document.querySelectorAll('#tasks-manual "
                    ".task-actions--pick input[type=checkbox]:checked').length"
                ),
                0,
            )
        )
        results.append(
            check(
                "未勾选时「一键执行」按钮禁用",
                page.evaluate(
                    "(() => { const b = document.getElementById('btn-run-manual');"
                    " return b ? b.disabled : 'missing'; })()"
                ),
                True,
            )
        )

        # 挑两个**非宴席**成员（宴席能不能吃取决于窗口，会让判据时灵时不灵）
        batch_picks = [
            name for name in batch_order.split(",") if name and name != "bbq_energy"
        ][:2]
        for name in batch_picks:
            page.evaluate(
                "(function(){ const box = document.querySelector("
                "window.__batchPickSel(" + json.dumps(name) + "));"
                " if (box && !box.checked) box.click(); return 'ok'; })()"
            )
        time.sleep(0.5)
        results.append(
            check(
                "勾选后按钮启用且带上数量",
                page.evaluate(
                    "(() => { const b = document.getElementById('btn-run-manual');"
                    " return Boolean(b && !b.disabled && /一键执行（\\d+）/.test(b.textContent)); })()"
                ),
                True,
            )
        )

        expected_steps = f"{','.join(batch_picks)},daily_box"
        batch_steps = ""
        for attempt in range(3):
            page.evaluate("document.getElementById('btn-run-manual').click(); 'ok'")
            for _ in range(20):
                batch_steps = page.evaluate(LATEST_RUN_STEPS_JS, await_promise=True)
                if batch_steps == expected_steps:
                    break
                time.sleep(1)
            if batch_steps == expected_steps:
                break
            print(f"   ↳ 第 {attempt + 1} 次一键执行未提交成功，重试…")
        print("   ↳ 一键执行的步骤：", batch_steps)
        results.append(
            check(
                "一键执行：批次末尾跟一次宝箱",
                batch_steps,
                expected_steps,
            )
        )

        # ★ 2026-10-16：把队列排起来，再比"界面 ⇔ 服务端"。
        # 单批 0.79 秒跑完，没有可观察窗口 —— 必须先连点制造稳定窗口，
        # 否则这条判据会"两边都是空集"地假通过（见 QUEUE_STREAK 的说明）。
        queue_streak(page)
        sync = sample_sync(page)
        print(
            f"   ↳ 与服务端不一致的按钮：{sync['bad'] or '（无）'}"
            f"｜服务端活动任务数：{sync['active']}"
        )
        results.append(
            check(
                "界面「执行中/排队中」与服务端 steps 一致（且有东西可比）",
                (sync["bad"], sync["active"] > 0),
                ("", True),
            )
        )
        # 双态按钮本身也是行为的一部分：有批次在跑/排队时它必须是「停止执行」。
        # 这条同时是下面"点停止"那条的前置条件 —— 单独断言能让失败可读。
        results.append(
            check(
                "有批次时「一键执行」按钮转为「停止执行」",
                wait_batch_stopping(page),
                True,
            )
        )

        # ★ 2026-10-16：**刷新页面后**界面仍与服务端一致。
        #
        # 【它治的是什么】「执行中/排队中」以前记在前端内存里（state.stopTasks），
        # 刷新页面就丢 —— 任务还在跑/排队，按钮却显示「执行」，看上去像"什么都没在跑"。
        # 现在这个态由 /api/status 派生，刷新后 loadTasks 装配完就该与服务端对齐。
        # 【为什么排在"停止"之前】刷新会重置 state.batchRunId，那之后就点不出
        # 「停止执行」了（按钮回到"一键执行"语义）—— 所以先验刷新，再验停止。
        page.call("Page.reload")
        wait_for_render(page)  # 同预检：等装配完再判，别让"页面还没起来"混进来
        after_reload = sample_sync(page)
        if after_reload["active"] == 0:
            # 队列在这几秒里放空了 → 再排一批，让这次比对有东西可比。
            # （"刷新那一下界面已经照服务端重建过"这件事仍然成立，这里补的是
            #   别让判据空转 —— 空转会伪装成通过。）
            print("   ↳ 刷新后队列已放空，补排一批再比…")
            queue_streak(page, 3)
            after_reload = sample_sync(page)
        print(
            f"   ↳ 刷新后与服务端不一致的按钮：{after_reload['bad'] or '（无）'}"
            f"｜服务端活动任务数：{after_reload['active']}"
        )
        results.append(
            check(
                "刷新页面后界面仍与服务端 steps 一致（且有东西可比）",
                (after_reload["bad"], after_reload["active"] > 0),
                ("", True),
            )
        )

        # ★ 2026-10-16：点「停止执行」→ 剩下的步骤该是 **skipped**（不是 fail：
        # 它不是做错了，是压根没做）。
        # 【为什么先排一批再点】单批 0.79 秒，等到按钮转成「停止执行」时它早跑完
        # 了 —— 那就"没东西可跳"，判据必然假红。先排一批（前一批在跑、后一批还在
        # 排队），此刻点停止一定会有一个批次被撤下：排队中的直接被摘掉标 skipped，
        # 正在跑的按协作式中断把没轮到的步骤标 skipped —— 两条路都留痕。
        skipped = 0
        for attempt in range(4):
            queue_streak(page, 4)
            if not wait_batch_stopping(page):
                print(f"   ↳ 第 {attempt + 1} 次：按钮没转成「停止执行」，重试…")
                continue
            page.evaluate("document.getElementById('btn-run-manual').click(); 'ok'")
            for _ in range(20):
                skipped = page.evaluate(
                    "fetch('/api/status').then(r => r.json())"
                    ".then(s => s.steps.filter(x => x.status === 'skipped').length)",
                    await_promise=True,
                )
                if skipped > 0:
                    break
                time.sleep(0.3)
            if skipped > 0:
                break
            print(f"   ↳ 第 {attempt + 1} 次：停止后仍没看到 skipped，重试…")
        print("   ↳ 被标记跳过的步骤数：", skipped)
        results.append(check("停止执行后剩余步骤被标为 skipped", skipped > 0, True))
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
