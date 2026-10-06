# 前端代码评审报告（2026-10-06，只读检查，未改动任何代码）

审查范围：`web/index.html`、`web/login.html`、`web/assets/{app,common,login}.js`、
`web/assets/{input,styles}.css`、`tailwind.config.js`、`main_gui.py` 注入脚本。
规模：JS 4,743 行 / CSS 产物 72 KB / 静态资源约 30 MB。

---

## 一、总体评价

**成熟度明显高于同类"顺手写的工具前端"**，属于有意识地做过架构取舍的产物。
判断依据（都是能直接验证的事实，不是印象分）：

| 维度 | 表现 |
| --- | --- |
| 依赖策略 | 零依赖、零运行时构建。改完刷新即见，断外网（局域网/安卓 WebView）不白屏 —— 决策与其部署场景匹配 |
| XSS 面 | 全站 DOM 走 `textContent` 构建（common.js 的 `el()`），**零处**把服务端文本当 HTML 解析。仅 `app.js:3673` 一处 `innerHTML` 且是硬编码 SVG 常量 |
| 状态管理 | 单一 `state` 对象 + 视图可从缓冲重建（`logLines` / `dockLines` / `runLines`），DOM 被清空也不丢历史 |
| WebView 适配 | 自绘 dialog 绕开 `alert/confirm` 被吞、自绘 select 绕开原生滚轮弹框、`?api=` 覆盖与 8000/8765 端口同源判断 —— 都是踩过坑才写得出来的 |
| 无障碍 | `aria-*` / `role` / `aria-selected` 与视觉态同步、3 处 `prefers-reduced-motion`、`:hover` 包在 `@media (hover:hover)` 里 |
| 主题 | head 内联脚本先落 `data-theme` 防闪白，CSS 统一 16 处 `[data-theme]` 选择器 |
| 注释 | 每个非常规决策都写了「为什么」（如 `ONCE_PER_DAY_TRIGGERS`、`REPEATABLE_AUTO` 的例外名单理由） |

一句话：**这是一个"用得很省但想得很清楚"的前端**，主要问题不在写得差，而在
「历史演进中留下的重复实现」与「构建链路的边界没覆盖到」。

---

## 二、必修（确认为真实缺陷）

### B1. `app.js::initWindowControls()` 是 68 行死代码，且与 Python 注入脚本重复

`web/assets/app.js:3647-3649` 取的是 `win-btn-min` / `win-btn-max` / `win-btn-close`
三个 **id**。全项目检索结果：这三个字符串**只出现在 app.js 的 `getElementById` 里**，
没有任何地方创建带这些 id 的元素 —— `main_gui.py` 的 `_WINDOW_CONTROLS_JS` 只设了
`box.id = "win-controls"`，按钮本身只有 `className`、没有 `id`；安卓端根本不注入窗口控制。

后果：
- 三个 `if (minBtn/maxBtn/closeBtn)` 分支**恒不成立**，其中整套动画 + `pywebview.api`
  调用 + `maxBtn.innerHTML` 切图标的逻辑从未执行过；
- 但函数末尾的 `window.addEventListener("focus", ...)`（3703 行）**是注册的**，
  而 `main_gui.py` 里有一份**逐字相同**的 focus 监听 —— 最小化恢复的回弹动画
  会由两处各跑一遍（重复 add/remove 同一批 class）。

实际功能没坏（main_gui.py 自己的 `make()` 里绑了完整的 click），所以是"静默冗余"，
但两份逻辑已经出现漂移（180/150/260ms 各写一遍），后续改一处必漏另一处。

验证方式：桌面端启动后在控制台跑 `document.getElementById("win-btn-close")` → `null`。

### B2. Tailwind 又摇掉了两个类，这次源头在 `web/` 之外（第 4 次中招）

`main_gui.py:88` 生成 `btn.className = \`iconbtn win-btn win-btn--${type}\``。
Tailwind 的 `content` 只扫 `./web/**/*.{html,js}` —— **Python 文件里的类名根本不在扫描范围**，
且 `win-btn--${type}` 又是动态拼接。实测构建产物 `styles.css`：

| 类名 | input.css 定义 | styles.css 产物 |
| --- | --- | --- |
| `.iconbtn` | 1 | **0** |
| `.win-btn--close` | 2 | **0** |
| `.win-btn` | 8 | 2（幸存） |

即：桌面 EXE 上窗口三键的 `.iconbtn` 基础样式与关闭键的红色 hover 态**从未生效过**。
修复方向（二选一）：把 `iconbtn` / `win-btn--close` 写进 `tailwind.config.js` 的 `safelist`，
或把 `main_gui.py` 加进 `content` 扫描路径 —— 后者更彻底，但要注意别把 Python 语法里的
噪音串误判成类名。

> 附带结论：本次对 `input.css` 的 219 个自定义类做了一次「定义 vs 产物」全量差集，
> 除上述两个外，其余 3 个缺失项（`btn--platform` / `tab-icon-img` / `task-note` /
> `topbar-logout`）在 `web/` 与 `.py` 中**均为 0 引用**，属改名后的遗留死代码，
> 建议直接删。动态拼接类 `stat--${tone}` 的 5 个变体、`step--${statusClass}` 的
> 5 个变体**都在产物里**（已逐一验证），这次没问题。

### B3. `call()` 没有超时，一次挂起请求可永久卡死整个轮询链

`common.js:170` 的 `call()` 直接 `fetch`，无 `AbortController`、无 `AbortSignal.timeout`
（全项目检索 `AbortController|timeout` → 0 命中）。而 `app.js:3301` 的 `pollOnce()` 用
`state.polling` 做并发守卫，`finally` 里才释放；`schedulePolling()` 是
「`await pollOnce()` 之后再排下一次」的递归 setTimeout。

故障链：服务端重启 / 手机切后台被系统掐断连接 / 局域网抖动 → 请求既不返回也不报错 →
`polling` 永远为 `true` → 后续每次 `pollOnce()` 在第 3306 行直接 `return` →
**进度、日志、宴席状态、自动补跑全部永久停止**，且界面上只表现为"连接点变灰"，
没有任何提示。这是三端里最难复现、但后果最严重的一类。

建议：`call()` 统一加超时（如 GET 8s / POST 30s），超时抛出的错误沿用现有
`describeError` 通路；`schedulePolling` 改成「无论 pollOnce 成败都排下一次」
（现在已经是 try/catch + finally，但要确认异常不会跳出递归）。

---

## 三、强烈建议（性能与体验）

### P1. 22 MB 的 OPPO Sans TTF，且存在两套字体栈

`web/assets/fonts/OPPO-Sans-4.0.ttf` 单文件 22 MB，`input.css:143` 声明为
`font-family: "OPPO Sans"`，`body` 把它排在最前 —— 首屏必然触发这 22 MB 的下载与解析。

问题有三层：
1. **格式**：TTF 未转 woff2（同字体 woff2 通常小 60%+）；
2. **未子集化**：游戏 UI 实际用到的字符集（常用汉字 + 数字 + 英文符号）子集化后可到 1~2 MB 量级；
3. **两套栈并存**（一致性问题）：`tailwind.config.js` 的 `fontFamily.sans` 里
   **没有 OPPO Sans**（`system-ui` 打头），而 `input.css` 的 `body` / `.topbar` /
   第 1395 行的 `!important` 规则都是 OPPO Sans 打头。
   结果：用 `font-sans` 工具类的元素走 system-ui 栈，body 走 OPPO Sans 栈 ——
   **同一页面上两套字形**。另外 `app.js:3496` 的注释已记录了"字体就位前后 tab 宽度会变"，
   说明这个大字体确实在造成布局抖动（CLS）。

建议优先级：**先统一字体栈**（把 OPPO Sans 写进 tailwind config 的 sans 首位，
或从 CSS 三处移除，二选一，别各留一套），再考虑 woff2 + 子集化。

### P2. 轮询不随页面隐藏而暂停

`schedulePolling()` 空闲 5s / 运行 1.2s 恒定轮询，无 `visibilitychange` 监听
（全项目 0 命中）。手机切到后台、桌面窗口最小化后仍在打 `/api/status` + `/api/logs`，
白耗电与 CPU。建议：页面隐藏时停表，恢复可见时立即补一次再续排。

### P3. `copyLog()` 的兜底与项目已知结论矛盾

`app.js:3569` 在剪贴板 API 失败时回退到 `window.prompt("手动复制下面的日志：", text)`。
但 `common.js:207-212` 的注释已经明确写下：Android WebView 无 `WebChromeClient` 时
会**静默丢弃** alert/confirm/prompt 这一族调用。而项目里**已有**
`copyToClipboard()`（app.js:2907，带 `execCommand` + 隐藏 textarea 兜底）却没复用它。
结果：手机上复制日志在最需要兜底的那一端正好失效，且静默无反馈。

建议：`copyLog()` 直接改调 `copyToClipboard()`，失败时走自绘 `uiAlert` 提示。

### P4. 缓存串号手工维护且已不同步

`index.html`：`styles.css?v=20261006_8`、`common.js?v=20261005_8`、`app.js?v=20261006_5`、
`login.js?v=20261006_2`。四个文件四个串号、手写维护，改了 `app.js` 忘了升串号
就会在手机上吃旧缓存（登录页注释里已经承认这是个坑）。
建议：串号由构建脚本统一注入，或改成文件内容 hash。

---

## 四、可维护性

### M1. 单文件体积
`app.js` 3,732 行 / 114 个函数塞在一个 IIFE 里，`renderInfo()` 单个函数 211 行。
按职责切分（信息面板 / 任务卡片 / 日志与底坞 / 轮询与提交 / 外观与窗口）后，
每个文件 300~600 行量级，可读性和改动安全性都会明显改善。

### M2. `tailwind.config.js` 的 `darkMode: "media"` 是死配置
CSS 里 `prefers-color-scheme` 出现 **0 次**，`dark:` 变体出现 **0 次**，
深色全部走 16 处 `[data-theme]` 选择器。也就是说配置文件声明的 media 策略
与实际实现的 data-theme 策略**完全不同源**。今天无害（没人写 `dark:`），
但下一个人写 `dark:bg-x` 会得到一个"手动切深色不生效、只有系统深色才生效"的诡异 bug。
建议改成 `darkMode: ["selector", '[data-theme="dark"]']` 或直接标注此处不可用。

### M3. 无前端测试
`tests/` 下只有 `test_main_gui.py` / `test_webapi_*.py`，**没有任何 JS/CSS 侧的测试**。
建议至少补两类低成本断言：
1. 构建产物守卫 —— 对一份"关键类名清单"（含 `iconbtn`、`win-btn--close`、
   `conn--ok/bad/busy`、`bbq-meal-btn--ready/eaten/not_started/expired`、
   `stat--gold/diamond/stamina/power`、`step--ok/fail/running/skipped/pending`）
   断言其在 `styles.css` 中出现次数 > 0。这一条能一次性防住已经中招 4 次的摇树问题；
2. `node --check` 三个 JS 文件的语法守卫（本次已手工跑过，三个均通过）。

### M4. 无全局错误处理
无 `window.onerror` / `unhandledrejection`。`app.js:281` 的 `void loadPlayer(...)`
和 2080 行的 `void loadSectionsForArea(...)` 若抛异常，只会在控制台留一条红字，
界面上毫无提示（用户看到的是"数据一直显示读取中"）。

---

## 五、无障碍（小修）

- **A1**：两个 HTML 都写了 `maximum-scale=1, user-scalable=no`，禁止双指缩放，
  违反 WCAG 1.4.4。工具型界面想保留这个行为可以理解，但建议至少放宽到
  `maximum-scale=5`，或仅在安卓 WebView 场景下保留。
- **A2**：全站**没有一个 `<h1>`**，标题层级从 `<h2>` 起（index 5 个、login 2 个），
  读屏与 Outline 导航拿不到文档主干。建议给每个分区面板补一个视觉隐藏的 h1/h2 层级。
- **A3**：`:focus-visible` 全站只出现 1 处。好消息是**没有任何 `outline:none` 清零**
  （0 命中），浏览器默认焦点环还在，所以键盘可用性没有崩；但自定义控件
  （自绘下拉、卡片按钮、主题卡）建议补上统一的 focus 环。

---

## 六、次要项

- 两个 `resize` 监听（3274 底坞钳位、3499 导航渐隐）都未节流；`scroll` 监听用的是
  `passive:true` 但同样未节流。成本不高，但加一层 rAF 合并是零风险的。
- `app.js:2823` 用 `Array.shift()` 裁剪 800 上限的日志缓冲：O(n) 搬运，
  800 元素下可忽略，不值得改。
- `styles.backup.css`（44 KB）已被 `.gitignore:103` 忽略 ✓ —— 这点做对了，
  否则改了 input.css 却对照 backup 排查会长期误导。
- 无 Service Worker：manifest 声明 `display: standalone`，但离线打开会白屏。
  本机服务场景影响有限，可暂不处理。

---

## 七、建议的整改顺序

1. **B2**（safelist 补两个类）+ **B1**（删死代码 / 去掉重复的 focus 监听）—— 各一行级改动，立竿见影；
2. **M3-1**（构建产物类名守卫测试）—— 把这个已经栽了 4 次的坑永久封死；
3. **B3**（fetch 超时）—— 唯一影响稳定性的项，值得单独一次改动 + 回归；
4. **P1**（字体栈统一 → woff2/子集化）、**P3**（复用 copyToClipboard）、**P2**（可见性暂停轮询）；
5. **M1**（拆文件）、**A1/A2**（无障碍）—— 属于打磨，可择机做。
