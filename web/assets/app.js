/* ============================================================
   米娅小助手 · 任务界面逻辑（index.html 专用，原生 JS）

   【两页分工（2026-09-30）】
       login.html：门 —— 令牌 → 登录 → 选区，成功后跳转过来；
       index.html ：屋子 —— 侧边栏五个分区 + 运行进度 + 实时日志。
   本文件只管"屋子"；共享工具（call / el / 令牌管理 / 防抖…）来自
   common.js 的 window.Ct —— 两页用同一份，避免协议性代码各抄一份后漂移。

   【2026-09-24：为什么界面上不再有任何"运行"按钮】
   两个分组的语义被彻底分开了，统一按钮在两边都会误导：

   - 自动任务（纯领取 / 只读）：**勾选即生效**。进主界面、勾选状态变动时
     都会自动排队执行（见 runAutomation）；同一个游戏日只跑一次
     （游戏服务器每天 05:00 换日），但「日常 / 周常活跃宝箱」例外 —— 它跟着
     主动任务补领，见 REPEATABLE_AUTO。
     ★ 2026-10-06：其中后端标了 ``once_per_day`` 的那批（礼包 / 工会签到 / 挖矿 /
     每日免费抽卡 / 荣耀之巅宝箱）**只在第一次登录工具时自动执行**，勾选变动不再
     捎带它们（见 ONCE_PER_DAY_TRIGGERS）；卡片上的「执行」小按钮不受此限。
   - 主动任务（会消耗资源）：卡片自己展开、自己确认 —— 点左侧 ▸ 展开参数，
     再点该卡片上的「执行这个任务」（执行前仍会二次确认危险参数）。

   【两条安全约定】
   - 令牌只进 sessionStorage（关标签页即失效），**不写 localStorage**；
   - 账号密码**完全不保存**：登录页提交后立刻清空输入框，用完即忘。

   【为什么一律用 textContent 拼 DOM】
   日志与服务端消息里会出现任意文本（角色名、错误信息…）。用 innerHTML 拼串
   等于把"服务端返回的内容"当代码执行，是典型的 XSS 入口。
   ============================================================ */
(() => {
  "use strict";

  // 共享工具来自 common.js（window.Ct）：call / el / 令牌管理 / 防抖 / 常量 ——
  // 两页（login.html / index.html）用同一份，改令牌头这类协议逻辑只改一处。
  // customSelect 也在其中（2026-10-05 上移）：登录页的账号选择同样要避开
  // Android 原生 select 那个排版错乱的系统弹框，所以它不再只属于本页。
  const {
    $,
    el,
    call,
    describeError,
    formatNumber,
    debounce,
    STORAGE_KEYS,
    AVATAR_PLACEHOLDER,
    uiAlert,
    customSelect,
  } = window.Ct;

  const state = {
    groups: [],
    tasks: [],
    views: new Map(), // 任务名 → /api/tasks 里的记录
    selection: new Map(), // 任务名 → { checked, params }
    player: null, // /api/player 的结果
    session: null, // /api/health 里的 session
    // 活动关卡候选缓存（★ 2026-09-22 新增）：来源标识 → 已拉到的候选数组。
    // 放在 state 而不是 DOM 里：任务卡片会被整体重建，<option> 会跟着消失，
    // 缓存放在这里才能在重建后立刻把上次拉到的候选填回去。
    activityOptions: {},
    // 各活动区域预加载的关卡候选全集（{ [area_id]: { sweepable, blocked } }）
    activityAllSections: {},
    // 后端回报的缓存元信息（"上次更新 / 下次自动更新"由后端算好，前端不碰时区）
    activityCache: null,
    // 被挡的关卡（{section_id, reason}）：界面要能解释"为什么这区只有 5 关可选"
    activityBlocked: [],
    // 素材关卡与元素试炼静态子目录树（GET /api/material/tree）
    materialTree: null,
    materialError: "",
    // 最近一次"取候选"的说明（人话）与错误
    activitySummary: "",
    activityError: "",
    // 荣耀之巅那份快照的读取时刻（见 loadPlayer：它不再参与常规刷新）
    topPvpAt: "",
    mainStageStatus: null,
    version: "",
    // ★ 自动任务（2026-09-24）：/api/tasks 带回的游戏日与"下次重置时刻"。
    // "今天跑过没有"完全依赖 gameDay —— 它就是"游戏服务器每天 05:00 换日"
    // 这条规则的**唯一**口径（后端算好，前端只比较字符串）。
    gameDay: "",
    nextReset: "",
    // ★ 2026-10-04：点了「执行」、但那批任务还没跑完的任务名。
    // 它们的按钮在执行期间显示成「停止执行」，点下去是中断而不是再跑一次。
    // 队列跑空（state.running 变 false）时整体清空，按钮自动复原。
    stopTasks: new Set(),
    // ★ 2026-10-04：服务端记录的"本游戏日已自动跑过"的任务名。
    // 它是最重要的那份判重依据 —— 浏览器 localStorage 按 origin 隔离
    // （换端口 / 换窗口就丢），所以真正的账记在服务端
    // （见 services/daily_state.py）。这里由 /api/tasks 与 /api/status 持续刷新。
    autoDone: new Set(),
    // ★ 2026-10-05：钻石总闸是否已从服务端还原过（服务端是权威，见 loadTasks）。
    // 还原成功后置 true，此后 runOptions 提交的 allow_diamond 才被视为
    // 用户此刻的明确选择（后端照单全收）；还原失败/旧 JS 时保持 false，
    // 后端改用「已保存值 或 本次请求值」合并，保住"开过就保持开"。
    allowDiamondSynced: false,
    // ★ 2026-10-05：宴席"早/晚两顿"的服务端状态。
    // ``{banquet_day, meals:[...]}`` 来自 /api/tasks 与 /api/status（服务端记账）；
    // ``banquetMeals`` 是 /api/banquet/status 拉到的**实时顿状态**
    // （{meal,label,state,edible,invitable,window_state,window_text,group_id} 数组）。
    // ★ 2026-10-06：面板只读 ``state``/``edible``/``invitable`` 渲染，不自己推窗口规则。
    // 两者分开：前者让按钮"吃过就灰"，后者让按钮"未到点/已过期"也有明确呈现。
    banquet: null,
    banquetMeals: null,
    // 已展开的主动任务名。只存内存：刷新页面后一律折叠 ——
    // 免得"上次点开的那张卡"看起来像"它还在执行/还在排队"。
    expanded: new Set(),
    // 「我的角色」分组是否展开全部。同样只存内存：名册动辄上百条，
    // 默认只铺一屏，用户想看全再点开（见 renderInfo 第 ④ 组）。
    rolesExpanded: false,
    // 运行中又触发了自动执行（例如刚登录就勾了一个任务）→ 先记下来，
    // 等当前这次跑完再补一次，而不是硬碰一个 HTTP 409。
    pendingAuto: false,
    // 最近一次提交的失败原因（显示在自动任务状态行上；成功一次就清掉）
    autoError: "",
    cursor: 0, // 日志增量游标
    running: false,
    pollTimer: null,
    // 失控炼成阵全服宝箱每小时轮询定时器（直到 3 份领满自动注销）
    yimoHourlyTimer: null,
    // ★ 轮询并发守卫（2026-09-30）：true = 已有一次 pollOnce 在飞。
    // 没有它，定时轮询未返回时 submitRun 又调一次，两次 fetchLogs 会用
    // 同一个 cursor 各取一遍 → 同一批日志行被追加两遍（见 pollOnce）。
    polling: false,
    // 日志缓冲（2026-10-01）：{ts, level, logger, message} 的环形队列，
    // 是三个视图（运行日志 / 开发者日志 / 底部坞）的唯一事实源 ——
    // 视图容器可以随时被清空/重建，缓冲在 state 里才丢不了历史。
    logLines: [],
    // 底部日志坞：收起时新到的"运行口径"行数（展开即清零）
    unread: 0,
    dockHeight: 300,
    // 结果流水（2026-10-01）：当前运行的 id 与已记录的步骤结果。
    // 运行视图每个任务只记一行 ✔/✘ —— 靠这两个字段去重，
    // 任务的 print 细节（脚本运行逻辑）不再进视图，只进「保存」导出的缓冲。
    lastRunId: null,
    renderedStepIds: new Set(),
    // ★ 2026-10-05：运行结果流水的**帧缓冲**。
    // 【为什么必须有它】底坞的 head（「运行结果」标题 + 清屏/收起）是上一批
    // 兄弟节点，而 appendLogView 的超限裁剪是 `removeChild(firstChild)` ——
    // 不看是不是日志行。只要历史行数触到 LOG_DOM_LIMIT，最先被砍掉的正是
    // 标题整块，dock 变成一个没有头、只有滚动的裸面板（双端都能复现）。
    // 改成"渲染到 DocumentFragment 帧缓冲 + 按文本去重后整体替换"后：
    //   1) 裁剪只可能发生在 .log-line 上，head/resizer 永远不会被误删；
    //   2) 帧缓冲里没有 .log-line 的兄弟节点可数，不会触到上限；
    //   3) 同一容器重复渲染同一行（多容器循环 + 重复 status）天然去重。
    dockLines: [],
    runLines: [],
  };

  //: 活跃宝箱任务名 —— 主动任务执行完之后要**补跑**的就是它（见 runManualTask）。
  const BOX_TASK = "daily_box";
  //: 与 services/task_spec.py 里同名任务的标题一致（按钮说明要写给人看）。
  const BOX_TASK_TITLE = "日常 / 周常活跃宝箱";
  //: 允许**重复执行**的自动任务：只有活跃宝箱。
  //:
  //: 【为什么用"例外名单"而不是"重复名单"】
  //: 默认安全：将来新增的自动任务不会因为忘了登记而天天重跑（那会让风控多看见
  //: 一堆包，用户也会淹没在"已领取"的失败行里）。活跃点是随主动任务上涨的，
  //: 所以只有宝箱需要"做完一个主动任务就再看一眼"。
  const REPEATABLE_AUTO = new Set([BOX_TASK]);

  //: 允许「每游戏日只自动跑一次」那批任务（后端 ``once_per_day``）自动提交的触发时机。
  //:
  //: 【为什么要有这个开关（★ 2026-10-06 用户要求）】
  //: 用户的原话是"这些任务只有第一次登录工具时执行"。它们本来靠 ``ranToday``
  //: 判重（服务端记账 + 本地记录）已经不会同日重跑，但**"勾选变动"**这条路径
  //: 仍可能把它们捎带提交一次：当天第一次打开时它们没勾上、之后随手勾上，
  //: 就会在那一刻跑掉 —— 那不是"第一次登录"。
  //: 所以现在明确：名单里的任务只跟"开机那一下"走（启动 / 登录成功 / 切区，
  //: 以及启动撞 409 之后的那次补跑）；勾选变动一律不捎带。
  //:
  //: ⚠️ **和主动执行不冲突**：卡片上的「执行」小按钮走 ``runSingleAutoTask``
  //: （``origin="manual"``），完全不经过这里 —— 任何时候点都照跑，优先级最高。
  const ONCE_PER_DAY_TRIGGERS = new Set(["启动", "补跑"]);

  //: 「我的角色」分组折叠时**最多铺几个角色**（一行；xl 下网格是 6 列）。
  //:
  //: 【为什么要有这个数】名册是 5011/1004 拉回来的全量角色，动辄几十上百条。
  //: 全铺开时这一栏比它上面三栏加起来还长，把「进度与探索」挤出首屏 ——
  //: 而这张面板叫「角色与资源」，角色只是其中一组，不该霸占整个版面。
  //: 超过这个数就收起来，标题右侧给一个「展开全部（共 N 个）」。
  const ROLE_PREVIEW_LIMIT = 6;

  /** 角色头像的最终 ``src``：有 code 用它，没有就回退统一占位图。
   *
   *  占位图常量在 ``common.js``（``AVATAR_PLACEHOLDER``）—— 玩家头像与角色
   *  头像用的是**同一张**自制图（★ 2026-10-06 用户拍板：没选头像的人与读取
   *  失败的人**一律看它**，不引入游戏官方默认剪影）。 */
  function roleAvatarSrc(iconCode) {
    return iconCode ? `assets/avatars/${iconCode}.png` : AVATAR_PLACEHOLDER;
  }

  // （$ / el / call / describeError / formatNumber / debounce / STORAGE_KEYS
  //   已上移到 common.js 的 window.Ct —— 见文件顶部的解构。）

  // ------------------------------------------------------------------
  // 屏幕切换与顶栏
  // ------------------------------------------------------------------
  // 顶栏连接状态点（2026-10-06 改版）：节点上**不再渲染任何可见文字**，
  // 只靠颜色判断 —— 灰 = 正在连接、红 = 连接失败、绿 = 连接成功、
  // 绿点呼吸 = 任务运行中（kind="busy"，CSS 见 input.css 的 .conn--busy）。
  // 文字没有真的丢掉，而是转到 title / aria-label：色盲用户与读屏仍有
  // 一条非视觉通道，鼠标悬停也能看到究竟卡在哪一步。
  // 同时把机器可读的状态挂到 data-state，将来要加提示或埋点不必反查 class。
  // ⚠️ 必须与 login.js::setConn 保持一致 —— 两页共用 .conn 的 CSS。
  function setConn(kind, text) {
    const node = $("conn");
    node.className = `conn conn--${kind}`;
    node.dataset.state = kind;
    const label = text || "连接状态";
    node.title = label;
    node.setAttribute("aria-label", label);
    // 防御「旧 HTML 缓存 + 新 JS」混搭：老页面里这个节点带着"已连接"文本，
    // 不清掉就会跟圆点挤在一起。清空是幂等的，正常运行下本来也没有子节点。
    node.textContent = "";
  }

  function currentPlayerLabel() {
    const session = state.session || {};
    if (!session.has_session) return "";
    const name = session.player_name || sessionStorage.getItem(STORAGE_KEYS.playerName) || `playerId=${session.player_id}`;
    return `${name} @ ${session.server_name || "?"}(#${session.server_id || "?"})`;
  }

  function renderIdentity() {
    const label = currentPlayerLabel();
    $("side-session").textContent = label;
    const topSession = $("topbar-session");
    if (topSession) {
      topSession.textContent = label;
      topSession.classList.toggle("hidden", !label);
    }
    $("app-version").textContent = state.version ? `v${state.version}` : "";
    $("about-version").textContent = state.version ? `v${state.version}（网页版）` : "";

    const session = state.session || {};
    $("about-session").textContent = session.has_session
      ? `${session.summary}（来源：${session.source}）`
      : "当前没有游戏会话状态；需要登录的任务会失败。";
  }

  // （访问令牌的 URL/会话存储交接在 common.js；本页不再放令牌输入卡 ——
  //   令牌缺失/失效一律回登录页 login.html 处理。）

  // ------------------------------------------------------------------
  // 启动：问服务端"会话还在不在"，不在就回登录页
  // ------------------------------------------------------------------
  async function loadHealth() {
    const payload = await call("/api/health");
    state.version = payload.app_version;
    state.session = payload.session || {};
    renderIdentity();
    return payload;
  }

  async function boot() {
    // ?flat=1 是**回退开关**：万一两页流程出问题，仍能跳过登录检查直接进本页
    const flat = new URLSearchParams(location.search).get("flat") === "1";
    try {
      setConn("wait", "正在连接…");
      const health = await loadHealth();

      // 未登录（或会话失效）→ 回登录页：登录 + 选区都在那边完成，
      // 成功后会话写回服务端并跳转回本页（见 login.js 的 doEnterServer）。
      if (!flat && health.next_screen !== "main") {
        location.href = "login.html" + location.search;
        return;
      }

      renderIdentity();
      renderInfo(); // 用已拿到的 health session 立即更新名片和面板骨架

      // 并行拉取任务清单与玩家数据 —— 但**自动任务不等信息面板**（2026-10-04 修）：
      // loadPlayer 要串行跑 5 个只读任务（实测 10~30 秒），过去 boot 等它才触发
      // runAutomation，结果"登录后自动任务迟迟不动"，用户随手拨一个开关
      // （那条路径不等 loadPlayer）立刻就跑 —— 被误判成自动执行坏了。
      // collectTasks 只依赖 loadTasks 产出的任务清单与勾选状态。
      const tasksReady = loadTasks();
      // ★ 登录进来的这次是"统一读取"：连荣耀之巅一起读一次
      // （之后就以这份快照为准，见 loadPlayer 的说明）。
      void loadPlayer({ full: true });
      await tasksReady;
      refreshMainStageStatus();
      // ★「每次启动脚本就自动执行」：进入主界面即触发一次自动任务
      // （今天已经跑过的会被 ranToday 挡掉，所以反复刷新页面不会重复发包）
      await runAutomation("启动");

      // 日志常驻：任务与后端动态都在这里看
      await pollOnce();
      schedulePolling();
    } catch (error) {
      if (error.status === 401) {
        // 令牌缺失/失效：清掉坏的，回登录页重新填
        window.Ct.clearToken();
        location.href = "login.html" + location.search;
        return;
      }
      setConn("bad", "服务不可用");
    }
  }

  // （账号库 / 账号密码登录 / 区服列表 / 进入游戏 —— 已整体移到
  //   login.html + login.js：那是"门"的职责，本页只服务已登录的会话。）

  // ------------------------------------------------------------------
  // 屏 ③：主界面（分区导航 / 信息展示 / 钻石使用）
  // ------------------------------------------------------------------
  function setTab(name) {
    if (name === "about") name = "settings";
    for (const button of $("tabs").querySelectorAll(".tab")) {
      const on = button.dataset.tab === name;
      button.classList.toggle("tab--on", on);
      // aria-selected 与视觉选中态同步（role=tab 在 index.html 的 #tabs 上）：
      // 读屏用户靠它才知道"现在停在哪个分区"，光有颜色差异是不够的。
      button.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const panel of document.querySelectorAll(".panel")) {
      panel.classList.toggle("hidden", panel.id !== `panel-${name}`);
    }
    const content = document.querySelector(".content");
    if (content) content.scrollTop = 0;
    sessionStorage.setItem(STORAGE_KEYS.tab, name);
    syncTabsScrollHint();
  }

  /**
   * 窄屏分区导航的「两端渐隐」开关（2026-10-06）。
   *
   * 为什么需要它：CSS 的 mask 渐隐擦的是**元素盒子两端**，跟滚动位置无关。
   * 于是滑到最右时，最后一个标签（「设置」）的右半截会被无条件擦掉 ——
   * 用户滑到头也看不全，报告成"有一部分被挡住"。正确做法是：渐隐只出现在
   * "确实还有内容"的那一侧，到头的哪一侧就关掉哪一侧。
   * 宽屏是纵向列表（不滚动）→ scrollWidth ≈ clientWidth → 两个类都挂上 → 无 mask。
   */
  function syncTabsScrollHint() {
    const strip = $("tabs");
    if (!strip) return;
    const max = strip.scrollWidth - strip.clientWidth;
    const x = strip.scrollLeft;
    // 容差 2px：高分屏 + 触摸滚动很难精确停在 0 / max 上
    strip.classList.toggle("is-at-start", x <= 2);
    strip.classList.toggle("is-at-end", max <= 2 || x >= max - 2);
  }

  /**
   * 一张统计卡（2026-09-30 增加图标与配色档位）。
   *
   * :param icon: emoji 图标（装饰用，标记 aria-hidden，读屏不念）。
   * :param tone: 值的配色档位（"gold" | "diamond" | "stamina" | "power"），
   *   由 styles.css 的 `.stat--<tone>` 决定颜色；缺省用默认前景色。
   *   【为什么放前端而不是后端】这是"数字重要程度的表达方式"，不是数据
   *   本身 —— 后端 /api/player 只描述事实，怎么呈现归界面管。
   */
  function statCard(label, value, hint, { icon = "", tone = "", extraClass = "" } = {}) {
    let iconEl = null;
    if (icon) {
      if (icon.includes("/") || icon.endsWith(".png")) {
        iconEl = el("img", { class: "stat-icon-img", src: icon, alt: "", "aria-hidden": "true" });
      } else {
        iconEl = el("span", { class: "stat-icon", text: icon, "aria-hidden": "true" });
      }
    }
    return el("div", { class: `stat ${tone ? `stat--${tone}` : ""} ${extraClass}`.trim() }, [
      el("div", { class: "stat-label" }, [
        iconEl,
        el("span", { text: label }),
      ]),
      el("div", { class: "stat-value", text: value }),
      hint ? el("div", { class: "stat-hint muted small", text: hint }) : null,
    ]);
  }

  function renderInfo() {
    const grid = $("info-grid");
    if (!grid) return;
    grid.textContent = "";
    const session = state.session || {};
    const player = state.player || {};
    const game = player.game || {};
    const roles = player.roles || {};
    const arena = player.arena || {};
    const wudou = player.wudou || {};
    const yimo = player.yimo || {};
    const topPvp = player.top_pvp || {};
    const hasData = Boolean(state.player && Object.keys(state.player).length > 0);

    // 角色名优先从后端 session / player 读取，其次回退 sessionStorage
    const name = session.player_name || player.player_name || sessionStorage.getItem(STORAGE_KEYS.playerName);

    // ① 顶部：玩家专属名片 Hero Card
    const heroCard = el("div", { class: "hero-player-card" }, [
      el("div", { class: "hero-player-identity" }, [
        el("div", { class: "hero-avatar" }, [
          // ★ 2026-10-05：优先真头像（登录 1004 编号 → services/avatar_service.py
          //   解析出的 data-url，/api/player 透出）。
          // ★ 2026-10-06：拿不到真图时回退**自制占位图**（原来回退默认立绘
          //   ``assets/icons/stats/player.png``）—— 用户拍板：没选头像的人与
          //   读取失败的人一律看这张图。``.hero-avatar img`` 用的是"完整容纳"
          //   布局，方图会整体显示、不会被裁掉边缘。
          el("img", {
            src: player.avatar_data_url || AVATAR_PLACEHOLDER,
            alt: "角色",
          }),
        ]),
        el("div", { class: "hero-names" }, [
          el("div", { class: "hero-player-name", text: name || (hasData ? "（未进过区）" : "读取中…") }),
          el("div", { class: "hero-tags" }, [
            el("span", { class: "hero-tag" }, [
              el("img", { src: "assets/icons/stats/server.png", alt: "" }),
              el("span", { text: session.server_name ? `#${session.server_id} ${session.server_name}` : (hasData ? "未连接区服" : "区服读取中…") }),
            ]),
            el("span", { class: "hero-tag" }, [
              el("img", { src: "assets/icons/stats/alliance.png", alt: "" }),
              el("span", { text: arena.alliance_name || (hasData ? "无公会" : "公会读取中…") }),
            ]),
          ]),
        ]),
      ]),
      el("div", { class: "hero-power-box" }, [
        el("span", { class: "hero-power-label" }, [
          el("img", { class: "stat-icon-img", src: "assets/icons/stats/power.png", alt: "" }),
          el("span", { text: "综合战力" }),
        ]),
        el("span", { class: "hero-power-val", text: formatNumber(arena.my_fighting) }),
      ]),
    ]);
    grid.appendChild(heroCard);

    // ② 分组一：核心资产（金币 / 钻石 / 体力）
    const assetTitle = el("div", { class: "stat-group-title", text: "核心资产" });
    const assetGrid = el("div", { class: "stat-grid stat-grid--assets" }, [
      statCard("游戏金币", formatNumber(game.coins), "", { icon: "assets/icons/stats/coin.png", tone: "gold", extraClass: "stat--asset" }),
      statCard("钻石", formatNumber(game.diamonds), "", { icon: "assets/icons/stats/diamond.png", tone: "diamond", extraClass: "stat--asset" }),
      statCard("体力", formatNumber(game.stamina), "", { icon: "assets/icons/stats/stamina.png", tone: "stamina", extraClass: "stat--asset" }),
    ]);
    grid.appendChild(assetTitle);
    grid.appendChild(assetGrid);

    // ③ 分组二：日常与竞技（竞技场 / 天命对决 / 失控炼成阵 / 荣耀之巅）
    const dailyTitle = el("div", { class: "stat-group-title", text: "日常与竞技" });
    const rankVal = formatNumber(topPvp.my_ranking);
    const pointsVal = formatNumber(topPvp.my_points);
    const rankText = (rankVal && rankVal !== "—") ? `第 ${rankVal} 名` : "—";
    // ★ 荣耀之巅不再跟着每次跑任务刷（2026-10-04）：副标题写明"这份数据
    // 是什么时候读的" —— 否则用户会以为是实时的，从而怀疑数字不对。
    const topPvpHint = state.topPvpAt ? `读取于 ${state.topPvpAt}` : "登录后统一读取";
    const subText = (pointsVal && pointsVal !== "—")
      ? `积分 ${pointsVal}｜${topPvpHint}`
      : topPvpHint;
    const dailyGrid = el("div", { class: "stat-grid stat-grid--daily" }, [
      statCard("竞技次数", formatNumber(arena.challenge_count), arena.buyed_count ? `已购 ${formatNumber(arena.buyed_count)} 次` : "", { icon: "assets/icons/stats/arena.png" }),
      statCard("天命对决次数", formatNumber(wudou.challenge_times), (hasData && wudou.my_ranking != null) ? `名次 ${formatNumber(wudou.my_ranking)}｜NPC ${formatNumber(wudou.npc_times)}` : "", { icon: "assets/icons/stats/wudou.png" }),
      statCard("失控炼成阵次数", formatNumber(yimo.remain_count), (hasData && yimo.now_score != null) ? `积分 ${formatNumber(yimo.now_score)}｜排名 ${formatNumber(yimo.now_rank)}` : "", { icon: "assets/icons/stats/yimo.png" }),
      statCard("荣耀之巅", rankText, subText, { icon: "assets/icons/stats/top_pvp_rank.png", tone: "power" }),
    ]);
    grid.appendChild(dailyTitle);
    grid.appendChild(dailyGrid);

    // ④ 分组三：进度与背包
    const mainStage = player.main_stage || state.mainStageStatus || {};
    const progTitle = el("div", { class: "stat-group-title", text: "进度与探索" });
    const progGrid = el("div", { class: "stat-grid stat-grid--progress" }, [
      statCard("最高主线通关", mainStage.highest_passed_desc || "—", mainStage.next_section_desc ? `下一关：${mainStage.next_section_desc}` : "", { icon: "assets/icons/stats/stage.png", tone: "stamina" }),
      statCard("背包条目", formatNumber(game.item_count), hasData ? "道具总种类数" : "", { icon: "assets/icons/stats/backpack.png" }),
    ]);
    grid.appendChild(progTitle);
    grid.appendChild(progGrid);

    // ⑤ 分组四：我的角色（★ 2026-10-05 新增）
    //
    // 数据来自只读任务 roles（5011 实时名册；拿不到时自动退回登录 1004 的快照）。
    // ★ 必须显示"这份数据是什么时候/从哪来的"：实时名册与登录快照的**新鲜度
    //   完全不同**，不说清楚用户会以为数字是实时的，从而怀疑数据不对
    //   （与荣耀之巅那张卡片同一套做法）。
    const roleItems = Array.isArray(roles.items) ? roles.items : [];
    const roleSourceText = roles.source === "live"
      ? "实时名册"
      : (roles.source === "snapshot" ? "登录快照" : "");
    // ★ 2026-10-06：名册是**全量**角色（5011 / 1004），几十上百条。
    //   全铺开会把这一栏撑得比上面三组加起来还长，把「进度与探索」挤出首屏。
    //   所以默认只铺一屏（前 ROLE_PREVIEW_LIMIT 个），其余收在标题右侧的
    //   「展开全部」后面；条数不超过一屏时没有折叠的必要，按钮不出现。
    const roleTotal = roleItems.length;
    const roleCollapsible = roleTotal > ROLE_PREVIEW_LIMIT;
    const rolesExpanded = roleCollapsible ? state.rolesExpanded : true;
    const roleShown = rolesExpanded ? roleItems : roleItems.slice(0, ROLE_PREVIEW_LIMIT);

    const roleTitle = el("div", { class: "stat-group-head" }, [
      el("div", { class: "stat-group-title", text: "我的角色" }),
      roleCollapsible
        ? el("button", {
            class: "stat-group-toggle",
            type: "button",
            text: rolesExpanded
              ? "收起"
              : `展开全部（共 ${formatNumber(roleTotal)} 个）`,
            title: rolesExpanded ? "收起角色列表" : "展开全部角色",
            "aria-expanded": rolesExpanded ? "true" : "false",
            onclick: () => {
              state.rolesExpanded = !state.rolesExpanded;
              renderInfo(); // 展开状态在 state 里，整块重绘不会丢
            },
          })
        : null,
    ]);
    const roleGrid = el("div", { class: "stat-grid stat-grid--roles" }, [
      statCard(
        "角色数量",
        hasData ? formatNumber(roles.count) : "—",
        roleSourceText ? `来源：${roleSourceText}` : (hasData ? "暂无角色数据" : "读取中…"),
        { icon: "assets/icons/stats/player.png" },
      ),
      ...roleShown.map((role) =>
        el("div", {
          class: "role-chip",
          title: role.name
            ? `${role.name}（实例 ${role.role_sid}）`
            : `角色实例 ${role.role_sid}`,
        }, [
          // ★ 2026-10-06：头像。``icon_code`` 是静态头像目录的文件名
          //   （如 ``a001_03h``，后端已做过存在性校验）；拿不到时给统一占位图
          //   —— 占位图的兜底**只在这里做一次**，后端保持"如实报告数据源缺口"
          //   的语义，不要再在接口层也兜一遍（双分支以后改不动）。
          el("img", {
            class: "role-chip-avatar",
            src: roleAvatarSrc(role.icon_code),
            alt: role.name || "角色",
            // 「展开全部」可能一次铺上百条，不加 lazy 会同时发上百个请求
            loading: "lazy",
            decoding: "async",
          }),
          el("span", {
            class: "role-chip-name",
            // 名字来自配置表；表读不到时后端给空串 —— 这时显示"未知角色"，
            // 绝不把裸 ID 当名字印在运行视图上（项目「运行视图纪律」）。
            text: role.name || "未知角色",
          }),
          el("span", {
            class: "role-chip-meta",
            text: `Lv.${formatNumber(role.level)}　★${formatNumber(role.star)}`,
          }),
        ]),
      ),
    ]);
    grid.appendChild(roleTitle);
    grid.appendChild(roleGrid);

    const updateEl = $("info-updated");
    if (updateEl) {
      updateEl.textContent = hasData
        ? `上次刷新：${new Date().toLocaleTimeString("zh-CN")}`
        : "数据读取中…";
    }
    const button = $("btn-refresh-info");
    if (button) {
      button.disabled = !hasData;
      button.textContent = hasData ? "刷新" : "读取中…";
    }

    const errors = player.errors || [];
    const box = $("info-errors");
    if (box) {
      box.textContent = errors.length ? `部分数据没取到（其余照常显示）：\n${errors.join("\n")}` : "";
      box.classList.toggle("hidden", errors.length === 0);
    }
  }

  /**
   * 拉一次「信息展示」快照。
   *
   * :param full: 是否**连带读取荣耀之巅**（``/api/player?full=1``）。
   *
   * 【★ 2026-10-04：荣耀之巅默认不再跟着读】
   * 以前这份快照里固定带着荣耀之巅的只读任务，而"每次运行结束"都会自动刷一次
   * —— 于是跑任何一个任务，日志里都会多出一段「跳过战斗演出…只读完成」。
   * 用户明确要求：**只有登录那次的统一读取、以及真正操作过荣耀之巅之后**才读它。
   * 所以默认 ``full=false``；``full=true`` 只在上述两个时机出现。
   *
   * 【为什么满了就要保留上一次的值】
   * ``full=0`` 的响应里 ``top_pvp`` 是空对象。直接盖上去会让面板突然显示"—"，
   * 看起来像"数据丢了"。保留上次读到的值（它是登录那一刻的真值），
   * 卡片上另有说明它的读取时刻。
   */
  async function loadPlayer({ full = false } = {}) {
    const button = $("btn-refresh-info");
    if (button) {
      button.disabled = true;
      button.textContent = "刷新中…";
    }
    const previousTopPvp = state.player && state.player.top_pvp;
    try {
      // 一次请求拿全（后端内部串行跑多个只读任务），避免"半新半旧"的显示。
      // 包含货币 / 竞技场 / 天命对决 / 失控炼成阵 / 主线最高进度只读；
      // 荣耀之巅只在 full=1 时才读（见上方说明）。
      const payload = await call("/api/player", {
        params: full ? { full: 1 } : null,
      });
      if (full) {
        state.topPvpAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      } else if (previousTopPvp) {
        payload.top_pvp = previousTopPvp;
      }
      state.player = payload;
      if (state.player && state.player.main_stage) {
        state.mainStageStatus = state.player.main_stage;
      }
      if (state.player && state.player.session) {
        state.session = state.player.session;
        renderIdentity();
      }
      renderInfo();
    } catch (error) {
      const box = $("info-errors");
      if (box) {
        box.textContent = `读取失败：${describeError(error)}`;
        box.classList.remove("hidden");
      }
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "刷新";
      }
    }
  }

  // （原 renderSpendInfo() 已随 2026-10-05 精简一并移除：它唯一的作用是把
  //   "总闸 / 宴席邀请 / 购买体力"的授权状态写进 `#spend-config` 那一行，
  //   而那一行按用户要求已从界面删除。`state.spend` 只被它写过、从未被读过，
  //   所以函数与字段一起删干净 —— 留着就是一段永远不会被看见的死代码。）

  function doLogout() {
    // "退出 / 换号" = 回登录页重走一遍（账号 → 选区），成功后会跳转回本页。
    // 注意：服务端的会话文件**不清除** —— 那是命令行与网页共用的状态，
    // 清掉会让 `python main.py run daily` 也跟着失效（见 notes/web_layer.md）。
    sessionStorage.removeItem(STORAGE_KEYS.playerName);
    location.href = "login.html" + location.search;
  }

  // ------------------------------------------------------------------
  // 任务清单：勾选状态（只存界面偏好，不存任何秘密）
  // ------------------------------------------------------------------
  function readStoredSelection() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.selection);
      return raw ? new Map(Object.entries(JSON.parse(raw))) : new Map();
    } catch {
      // 存坏了就当没有：界面偏好丢了无所谓，绝不能因此让页面打不开
      return new Map();
    }
  }

  function saveSelection() {
    const plain = {};
    for (const [name, entry] of state.selection) plain[name] = entry;
    try {
      localStorage.setItem(STORAGE_KEYS.selection, JSON.stringify(plain));
    } catch {
      /* 隐私模式 / 存储被禁用时忽略 */
    }
    // ★ 2026-10-05：双写服务端（localStorage 从"唯一来源"退化为兜底）。
    // 服务端是权威账本 —— EXE 的 Edge --app 壳把 profile 放 tmp/ 下，
    // 清理即丢（与钻石总闸、autoDone 判重同一教训）。
    scheduleSelectionSync();
  }

  // ------------------------------------------------------------------
  // selection 服务端同步：500ms 防抖（2026-10-05）
  //
  // 【为什么要防抖】saveSelection 的调用点里有参数输入框的 input 事件 ——
  // 用户在"扫荡次数"里敲一个 3 位数会触发三次保存；不加防抖就是三个请求。
  // 500ms 内的连续变更合并成一次 POST（提交的永远是 state.selection 全量）。
  //
  // 【演练开关为什么也走这里】POST /api/selection 的 body 是
  // {tasks, dry_run} 全量 —— dryRun 勾选变化后借下一次同步顺带保存，
  // 不用单独开接口。运行时是否演练由 isDevMode() 硬闸决定，与这份记录无关。
  //
  // 【失败为什么只 console.warn 而不弹窗】保存请求紧跟每次击键/勾选，
  // 弹窗会轰炸；丢了顶多回到默认勾选（无害方向）。钻石总闸弹窗是因为
  // 那是"花钱的闸"，必须可见 —— 两者的失败严重度不同，处理也不同。
  // ------------------------------------------------------------------
  let selectionSyncTimer = null;
  function scheduleSelectionSync() {
    if (selectionSyncTimer) clearTimeout(selectionSyncTimer);
    selectionSyncTimer = setTimeout(async () => {
      selectionSyncTimer = null;
      const tasks = {};
      for (const [name, entry] of state.selection) tasks[name] = entry;
      try {
        await call("/api/selection", {
          method: "POST",
          body: { tasks, dry_run: isDryRun() },
        });
      } catch (error) {
        console.warn("任务勾选状态没能保存到服务端：", describeError(error));
      }
    }, 500);
  }

  // ------------------------------------------------------------------
  // 运行模式：演练（不发包） vs 真实执行
  //
  // 【为什么要把选择记在本地（2026-09-22 修）】
  // "默认演练"是刻意的安全设计（见 webapi/app.py 的 suggested_dry_run），
  // 但过去 loadTasks() **每次**都无条件把开关设回演练 —— 而 loadTasks() 在
  // 「进主屏」和「登录成功后」各调一次。于是用户关掉演练、跑一次，
  // 再回主屏或重新登录，开关又自己打开了：任务显示绿色 ✅ 却什么也没发生。
  // ------------------------------------------------------------------
  // 开发者模式与运行模式（默认真实执行；?dev=1 时显示演练开关供验证）
  // ------------------------------------------------------------------
  function isDevMode() {
    try {
      const params = new URLSearchParams(window.location.search);
      return params.get("dev") === "1" || params.get("dev") === "true";
    } catch {
      return false;
    }
  }

  function readStoredDryRun() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.dryRun);
      if (raw === "true") return true;
      if (raw === "false") return false;
      return false; // 默认真实执行
    } catch {
      return false;
    }
  }

  function saveStoredDryRun(value) {
    try {
      localStorage.setItem(STORAGE_KEYS.dryRun, value ? "true" : "false");
    } catch {
      /* 隐私模式 / 存储被禁用时忽略 */
    }
  }

  /**
   * 读取"钻石使用总闸"上次的本地选择（2026-10-05）。
   *
   * 【注意：这只是兜底，不是权威】权威在服务端（/api/tasks 的
   * `spend.saved_allow_diamond`，见 loadTasks）。EXE 的 Edge --app 窗口
   * 把 profile 放在 tmp/ 下，一清理 localStorage 就全丢 —— 这个教训
   * 与 autoDone 判重完全相同。本地这份只在服务端字段缺失时顶上。
   *
   * @returns {boolean} 上次本地选择；从未设置过（或存储不可用）时返回 ``false``
   *   —— 默认仍然是"关闭"，与 config 侧的 fail-closed 一致。
   */
  function readStoredAllowDiamond() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.allowDiamond);
      if (raw === "true") return true;
      if (raw === "false") return false;
      return false; // 从没设置过 = 默认关闭（保守）
    } catch {
      return false;
    }
  }

  function saveStoredAllowDiamond(value) {
    try {
      localStorage.setItem(STORAGE_KEYS.allowDiamond, value ? "true" : "false");
    } catch {
      /* 隐私模式 / 存储被禁用时忽略：只是记不住偏好，不影响本次运行 */
    }
  }

  function isDryRun() {
    if (!isDevMode()) return false;
    const checkbox = $("opt-dry-run");
    return checkbox ? Boolean(checkbox.checked) : false;
  }

  function runModeText() {
    return isDryRun() ? "演练（不发包）" : "真实执行";
  }

  /** 进度卡上的模式标记：在 dev 模式或演练模式下显示。 */
  function renderProgressMode(dryRun) {
    const node = $("progress-mode");
    if (!node) return;
    if (typeof dryRun !== "boolean") {
      node.classList.add("hidden");
      return;
    }
    if (!isDevMode() && !dryRun) {
      node.classList.add("hidden");
      return;
    }
    node.textContent = dryRun ? "演练（未发包）" : "真实执行";
    node.className = dryRun ? "badge badge--warn" : "badge badge--ok";
    node.classList.remove("hidden");
  }

  /**
   * 把"存下来的勾选"与"当前任务规格"对齐。
   *
   * 后端可能新增参数或删掉任务。对齐之后：新参数取默认值、消失的任务自动丢弃、
   * 勾选状态按名字继承 —— 这就是"给后续扩展留空间"的落点。
   *
   * 【为什么内部参数（``hidden``）要跳过（★ 2026-10-06）】
   * 这里填的初值会被原样塞进请求体（``tasks.push({params: {...entry.params}})``）。
   * 若把 ``prefetch_sections`` 也填成 ``false`` 一起发出去，虽然结果无害，
   * 但它就成了"网页在替用户表达一个用户没做过的决定" ——
   * 让它缺席，由后端补默认值，语义才干净（见 ``visibleParams``）。
   */
  function reconcileSelection() {
    const stored = readStoredSelection();
    state.selection = new Map();
    for (const task of state.tasks) {
      const old = stored.get(task.name) || {};
      const params = {};
      for (const param of visibleParams(task)) {
        const fallback =
          param.default === null || param.default === undefined ? "" : param.default;
        params[param.name] =
          old.params && Object.prototype.hasOwnProperty.call(old.params, param.name)
            ? old.params[param.name]
            : fallback;
      }
      state.selection.set(task.name, {
        checked: typeof old.checked === "boolean" ? old.checked : Boolean(task.default_checked),
        params,
      });
    }
    saveSelection();
  }

  // ------------------------------------------------------------------
  // 渲染：自动任务 / 主动任务 / 钻石视图
  // ------------------------------------------------------------------
  function renderTaskGroups() {
    renderTaskGroup("tasks-auto", "auto");
    renderTaskGroup("tasks-manual", "manual");
    renderDiamondList();
    renderAutoStatus();
  }

  function renderTaskGroup(containerId, groupKey) {
    const container = $(containerId);
    container.textContent = "";
    // ``hidden`` 的任务（如 login）由专用界面负责，不出现在清单里
    const members = state.tasks.filter((task) => task.group === groupKey && !task.hidden);
    if (members.length === 0) {
      // .empty：空状态专用排版（居中 + 图标 + 留白），比一句灰字更"被设计过"
      container.appendChild(el("p", { class: "empty muted", text: "🗄️（暂无任务）" }));
      return;
    }
    for (const task of members) container.appendChild(renderTask(task));
  }

  const TASK_ICONS = {
    mail: "assets/icons/tasks/mail.png",
    free_gift: "assets/icons/tasks/free_gift.png",
    alliance_sign_in: "assets/icons/tasks/alliance_sign_in.png",
    alliance_mining_personal: "assets/icons/tasks/alliance_mining_personal.png",
    alliance_mining_team: "assets/icons/tasks/alliance_mining_team.png",
    alliance_donate: "assets/icons/tasks/alliance_donate.png",
    grand_line_supply: "assets/icons/tasks/grand_line_supply.png",
    daily_box: "assets/icons/tasks/daily_box.png",
    sweep_activity: "assets/icons/tasks/sweep_activity.png",
    sweep_material: "assets/icons/tasks/sweep_material.png",
    market_buy: "assets/icons/tasks/market_buy.png",
    top_pvp_battle: "assets/icons/tasks/top_pvp_battle.png",
    bbq_energy: "assets/icons/tasks/bbq_energy.png",
    buy_energy: "assets/icons/tasks/buy_energy.png",
    push_main_stage: "assets/icons/tasks/push_main_stage.png",
    sign_in: "assets/icons/tasks/sign_in.png",
    daily_free_draw: "assets/icons/tasks/daily_free_draw.png",
  };

  /**
   * 钻石使用页的任务视图：**只读展示**，不重复放一套控件。
   *
   * 【为什么不复用主动任务那套卡片】
   * 同一个任务（例如宴席）出现在两个分区里、各有一份勾选框与参数输入框时，
   * 两边会各自更新自己的 DOM —— 用户改了一处、另一处还显示旧值，
   * 看起来就是"这个开关坏了"。所以这里只把"与钻石有关的部分"集中念一遍。
   */
  function renderDiamondList() {
    const container = $("tasks-diamond");
    container.textContent = "";
    const members = state.tasks.filter(
      (task) => !task.hidden && (task.spends_diamond || (task.diamond_purposes || []).length)
    );
    if (members.length === 0) {
      container.appendChild(el("p", { class: "empty muted", text: "💤（当前没有会涉及钻石的任务）" }));
      return;
    }
    for (const task of members) {
      const entry = state.selection.get(task.name);
      container.appendChild(
        el("div", { class: "task" }, [
          el("div", { class: "task-title" }, [
            TASK_ICONS[task.name]
              ? el("img", { class: "task-icon-img", src: TASK_ICONS[task.name], alt: "", "aria-hidden": "true" })
              : null,
            el("span", { text: task.title }),
            // "勾选"这个词在新交互里已经不存在了（2026-09-24）：自动任务靠勾选启用、
            // 主动任务靠展开确认。所以这里按分组给出各自的真实状态。
            entry && entry.checked
              ? el("span", { class: "badge badge--ok", text: "已启用（自动执行）" })
              : task.group === "manual"
                ? el("span", { class: "badge badge--warn", text: "需在卡片里确认执行" })
                : el("span", { class: "badge", text: "未启用" }),
            task.diamond_purposes && task.diamond_purposes.length
              ? el("span", { class: "badge badge--danger", text: "有花钱分支" })
              : null,
          ]),
          el("p", { class: "task-sub", text: task.summary }),
          // ★ 2026-10-05 用户需求：原本这里还会逐个列出"参数名：当前值"
          // （例如"购买体力次数：0"）。那行只是把参数区已有的信息重念一遍，
          // 既不参与操作、也不解释风险 —— 用户明确要求删掉。
          // 保留的三样：任务名、真实状态徽标、一句话说明。
        ])
      );
    }
  }

  function taskBadges(task) {
    const badges = [];
    if (!task.enabled) {
      badges.push(el("span", { class: "badge badge--danger", text: "未实现" }));
    } else if (!task.requires_auth) {
      badges.push(el("span", { class: "badge badge--ok", text: "免登录" }));
    }
    // 主动任务（group === "manual"）不展示"可能花钻石"和"有消耗"提醒（用户需求）
    if (task.group !== "manual") {
      if (task.spends_diamond || (task.diamond_purposes && task.diamond_purposes.length)) {
        badges.push(el("span", { class: "badge badge--danger", text: "可能花钻石" }));
      }
      if (task.consumes) badges.push(el("span", { class: "badge badge--warn", text: "有消耗" }));
    }
    return el("div", { class: "badges" }, badges);
  }

  /**
   * 一张任务卡片。★ 2026-09-24：按分组生成**两套不同的头部控件与正文**。
   *
   * - 自动任务（group=auto）：左侧是复选框 = "纳入每日自动执行"。勾上就等自动跑
   *   （进主界面 / 登录成功 / 切换区服 / 勾选变动时触发，见 runAutomation）。
   * - 主动任务（group=manual）：左侧是 ▸ 折叠箭头。展开后才显示参数与
   *   「执行这个任务」按钮 —— 会花体力 / 次数的动作必须当场确认一次。
   *
   * 【为什么主动任务不再有复选框（用户 2026-09-24 要求）】
   * "勾选"的语义是"等着系统去做"，而主动任务需要的是"现在我明确要做"。
   * 用同一个控件表达两种相反的意图，误解的代价是真实的资源被花掉。
   *
   * 【参数区为什么默认隐藏】
   * 手机上少一半滚动距离（原设计保留）：自动任务未勾选时不显示，
   * 主动任务未展开时不显示。
   */
  //: 宴席卡片里由**专用双按钮界面**接管、不再走通用参数渲染的参数名（2026-10-05）。
  //: ``meal`` 由两个按钮各自指定；两个邀请开关画在各自按钮旁边。
  //: ``invite_friends`` 是整场/CLI 的老开关，网页里不再单独出现
  //: （网页一律走带 ``meal`` 的分顿路径）。
  const BBQ_CUSTOM_PARAMS = new Set([
    "meal",
    "invite_morning",
    "invite_evening",
    "invite_friends",
  ]);

  /**
   * 这个任务**该画出来**的参数（★ 2026-10-06）。
   *
   * 规格表里 ``hidden: true`` 的参数（目前只有 ``sweep_activity.prefetch_sections``）
   * 是进程内调用方用的实现细节（缓存预热）：既不画在表单里，也**不该**被塞进
   * 请求体 —— 让它走"没提供"的默认值即可（后端 ``normalize_params`` 会补 ``false``）。
   *
   * 【为什么集中成一个函数】
   * 判定要在两处用（渲染循环 + ``reconcileSelection`` 的初值填充）。
   * 分开各写一遍 ``!param.hidden`` 的话，将来只改了一处就会变成
   * "界面上看不见、请求里却一直带着 true"这种最难查的错。
   */
  function visibleParams(task) {
    return task.params.filter((param) => !param.hidden);
  }

  function renderTask(task) {
    const entry = state.selection.get(task.name);
    const isManual = task.group === "manual";
    const expanded = isManual && state.expanded.has(task.name);
    const bodyOpen = isManual ? expanded : entry.checked;
    const shownParams = visibleParams(task);

    const paramsBox = el("div", { class: "task-params" });
    paramsBox.classList.toggle("hidden", !(bodyOpen && shownParams.length > 0));
    if (task.name === "push_main_stage") {
      paramsBox.appendChild(renderMainStageBanner());
    } else if (task.name === "sweep_activity") {
      // 「更新区域」收进展开栏（2026-10-03 用户要求）：标题行只留标题与说明
      paramsBox.appendChild(activityRefreshTools());
    }
    for (const param of shownParams) {
      // 宴席的四个参数交给下面的专用双按钮界面，不在这里重复画一遍
      if (task.name === "bbq_energy" && BBQ_CUSTOM_PARAMS.has(param.name)) continue;
      paramsBox.appendChild(renderParam(entry, param));
    }

    const body = [paramsBox];
    if (isManual && expanded) {
      body.push(
        ...(task.name === "bbq_energy" ? bbqMealControls(task) : manualExecControls(task))
      );
    }

    const hasCandidates = shownParams.some((param) => param.options_source);
    const headControl = isManual
      ? manualToggle(task, expanded)
      : autoControls(task, entry, hasCandidates);

    // 头部布局（2026-10-01）：主动任务 = ▸ 展开箭头在左（点卡片左边展开的
    // 肌肉记忆不变）；自动任务 = 信息靠左、拨动开关在最右。
    const taskIcon = TASK_ICONS[task.name];
    const info = el("div", { class: "task-info" }, [
      el("div", { class: "task-title" }, [
        taskIcon
          ? el("img", { class: "task-icon-img", src: taskIcon, alt: "", "aria-hidden": "true" })
          : null,
        el("span", { text: task.title }),
        taskBadges(task),
      ]),
      task.summary ? el("p", { class: "task-sub", text: task.summary }) : null,
    ]);

    const head = el(
      "div",
      { class: `task-head ${isManual ? "task-head--manual" : "task-head--flip"}` },
      isManual ? [headControl, info] : [info, headControl]
    );
    if (isManual && task.enabled) {
      head.addEventListener("click", (event) => {
        if (event.target.closest("button, input, select, a, label")) return;
        if (state.expanded.has(task.name)) {
          state.expanded.delete(task.name);
        } else {
          state.expanded.add(task.name);
          // ★ 展开宴席卡片时顺带联网读一次两顿的实时窗口（否则按钮不知道能不能点）。
          // 不每次都拉：只在"还没读过"时拉一次，之后的刷新由面板上的「刷新」按钮负责，
          // 免得每展开一次就多发两个包（32007/32008 也算风控可见的流量）。
          if (task.name === "bbq_energy" && !state.banquetMeals) {
            refreshBanquetStatus(null);
          }
        }
        renderTaskGroups();
      });
    }

    return el(
      "div",
      {
        class: `task ${bodyOpen ? "task--on" : ""} ${expanded ? "task--expanded" : ""} ${
          task.enabled ? "" : "task--off"
        }`,
      },
      [
        head,
        ...body,
      ]
    );
  }

  /**
   * 自动任务右侧操作区：一个小的主动执行按钮 + 每日自动执行的拨动开关。
   *
   * 【为什么每个任务加单独的执行按钮（2026-10-03 用户需求）】
   * 满足特殊情况下的手动执行需求（如网络闪断后单项补领、掉线重领、或无需触发全部自动任务的即时执行）。
   */
  function autoControls(task, entry, hasCandidates) {
    const execBtn = el("button", {
      type: "button",
      class: "task-auto-exec",
      text: execLabel(),
      title: `主动执行「${task.title}」`,
      "aria-label": `主动执行${task.title}`,
      "data-task": task.name,
    });
    // ★ 2026-10-04：执行中**不再禁用** —— 点了就排队（见 onExecClick）。
    execBtn.disabled = !task.enabled;
    execBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      onExecClick(task, execBtn);
    });

    const checkbox = autoCheckbox(task, entry, hasCandidates);
    return el("div", { class: "task-actions" }, [execBtn, checkbox]);
  }

  /**
   * 自动任务的复选框：勾选 = 纳入每日自动执行。
   *
   * 【为什么勾选后**立刻**触发一次自动执行】
   * 用户的预期是"勾上就该跑"。只等下次刷新页面才跑，会让人以为勾选没生效。
   * 今天已经跑过的任务会被 ranToday() 挡掉，所以这里不必担心重复发包。
   */
  function autoCheckbox(task, entry, hasCandidates) {
    const checkbox = el("input", { type: "checkbox", class: "toggle" });
    checkbox.checked = entry.checked;
    checkbox.disabled = !task.enabled;
    checkbox.setAttribute("aria-label", `启用${task.title}`);
    checkbox.addEventListener("change", async () => {
      const wanted = checkbox.checked;
      entry.checked = wanted;
      saveSelection();
      // 整组重渲染：卡片高亮、参数区显隐、钻石页、状态行一次性对齐
      renderTaskGroups();
      // 勾选时若还没有候选（例如启动时未登录，或从未拉过），补一次自动拉取：
      // 命中缓存时它是零请求，只有真的没有数据才会联网。
      if (wanted && hasCandidates && activityOptions("activity_areas").length === 0) {
        await ensureActivityCache();
        renderTaskGroups();
      }
      // ★ 勾选本身就是一次"状态变动" → 让自动执行编排判断一次
      // （今天没跑过就马上跑；跑过则什么也不发，见 ranToday）。
      if (wanted) await runAutomation("勾选变动");
      else renderAutoStatus();
    });
    return checkbox;
  }

  /**
   * 主动任务的展开箭头（原来这个位置放的是复选框）。
   *
   * 展开状态记在 ``state.expanded``（只在内存里）：刷新页面后一律折叠 ——
   * 否则"上次点开的那张卡"看起来像"它还在排队执行"。
   */
  function manualToggle(task, expanded) {
    const button = el("button", {
      class: "task-toggle",
      type: "button",
      text: "▸",
      title: expanded ? "收起这张卡片" : "展开：看参数并单独执行",
    });
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    button.disabled = !task.enabled;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      if (state.expanded.has(task.name)) state.expanded.delete(task.name);
      else state.expanded.add(task.name);
      renderTaskGroups(); // 展开状态在 state 里，重建卡片不会丢
    });
    return button;
  }

  /**
   * 主动任务展开后的正文尾部：一行说明 + 「执行这个任务」按钮。
   *
   * 【为什么要写那行说明】
   * 执行按钮提交的是「这个任务 + 活跃宝箱」两步（见 runManualTask）。
   * 不解释的话，用户看到日志里多出一段宝箱输出会以为点错了。
   */
  /**
   * 宴席专用：**左右两个按钮**（早宴席 / 晚宴席），各带一个邀请开关（2026-10-05）。
   *
   * 【为什么要为一张卡片写专用界面】
   * 宴席一天两顿、协议上是两个包（15003 带不同 groupID），一顿只能吃一次。
   * 通用的"一个执行按钮"无法表达"这一顿能不能点、吃没吃过、要不要邀请"，
   * 所以按 ``renderMainStageBanner`` 的先例做一张**专用面板**。
   * 这也正是"协议与界面一一对应"：一个包 ↔ 一个按钮。
   *
   * 【状态怎么来（★ 2026-10-06 第七版）】
   * 后端 ``/api/banquet/status`` 每顿都给 ``state`` / ``edible`` / ``invitable``
   * （见 ``tasks/bbq.py::_meals_view``），前端**只负责渲染**、不自己推规则 ——
   * 第七版最容易搞错的就是"吃"与"邀请"的窗口不一样，两处各实现一遍必然打架。
   *
   * 【四种状态（每个按钮独立判定）】
   * - ``eaten``      ：本宴席日这顿已吃过（服务端记账）→ 灰、不可点；
   * - ``gap``        ：06:00–06:10 空档期（晚宴席已结束、早宴席未刷新）→ 灰、不可点；
   * - ``ready``      ：正开放 → 可点，**邀请开关可用**；
   * - ``makeup``     ：**补吃**（早宴席已过自己的窗口）→ 可点，但邀请开关**置灰**；
   * - ``not_started``/``expired``：晚宴席未开始 / 已结束 → 灰、不可点；
   * - ``unknown``    ：还没联网读到时段 → 允许点（后端仍会按真实窗口把关）。
   *
   * 【两个邀请开关各自独立】
   * 开关状态存在 ``entry.params.invite_morning`` / ``invite_evening`` 里，随按钮一起提交。
   * **窗口外时开关直接置灰不可操作**（用户要求"让用户直观知道这条路被强制屏蔽"），
   * 后端另有一道闸门：窗不覆盖一律走免费档（双保险）。
   */
  /**
   * 宴席面板的**状态 → 类名**映射（★ 必须写成字面量，理由见下）。
   *
   * 【为什么不能拼 `bbq-meal-btn--${stateKey}`】
   * Tailwind 3 的 JIT 靠**扫描源文件里的字面量**决定生成哪些类。拼出来的类名在源文件里
   * 根本不存在，于是 `input.css` 里那几条状态配色规则会被**整段摇掉**，一条都进不了
   * `styles.css`。实测（2026-10-06）：改成映射表之前，
   * `bbq-meal-btn--ready/eaten/not_started/expired` 在 `styles.css` 里出现 **0 次** ——
   * 也就是说**四种状态的颜色从来没生效过**，按钮一直只有 `.btn` 的默认样式。
   * 写成字面量（哪怕只是放在表里）就能被扫到，规则才会真的产出。
   */
  const BBQ_MEAL_STATE_CLASS = {
    eaten: { panel: "bbq-meal--eaten", button: "bbq-meal-btn--eaten" },
    gap: { panel: "bbq-meal--gap", button: "bbq-meal-btn--gap" },
    ready: { panel: "bbq-meal--ready", button: "bbq-meal-btn--ready" },
    makeup: { panel: "bbq-meal--makeup", button: "bbq-meal-btn--makeup" },
    not_started: { panel: "bbq-meal--not_started", button: "bbq-meal-btn--not_started" },
    expired: { panel: "bbq-meal--expired", button: "bbq-meal-btn--expired" },
    unknown: { panel: "bbq-meal--unknown", button: "bbq-meal-btn--unknown" },
  };

  function bbqMealControls(task) {
    const entry = state.selection.get(task.name);
    const doneMeals = new Set(
      (state.banquet && state.banquet.meals) || []
    );
    const live = Array.isArray(state.banquetMeals) ? state.banquetMeals : null;

    const row = el("div", { class: "bbq-meals" });
    // 顶部一行：状态说明 + 「刷新」按钮（点它联网问一次两顿的实时窗口）
    const statusText = live
      ? `宴席日 ${state.banquet ? state.banquet.banquet_day : ""}`
      : "尚未读取宴席时段（展开卡片或点「刷新」）";
    const refresh = el("button", {
      class: "btn btn--ghost bbq-refresh",
      type: "button",
      text: "刷新",
      title: "联网读取两顿宴席的开放时间",
    });
    refresh.addEventListener("click", async () => {
      await refreshBanquetStatus(refresh);
    });
    row.appendChild(
      el("div", { class: "bbq-meals-head" }, [
        el("span", { class: "bbq-meals-day", text: statusText }),
        refresh,
      ])
    );

    const grid = el("div", { class: "bbq-meals-grid" });
    for (const meal of ["morning", "evening"]) {
      grid.appendChild(bbqMealPanel(task, entry, meal, { live, doneMeals }));
    }
    row.appendChild(grid);
    return [row];
  }

  /**
   * 单顿面板：标题 + 邀请开关 + 执行按钮。
   *
   * 【状态判定顺序】吃过 > 后端状态 > 未联网（``unknown``）。
   * 后端 ``state`` 已经是最终结论，这里只做"吃过"这一层的覆盖 ——
   * 记账在前端只可能更"晚"知道，不能更早。
   */
  function bbqMealPanel(task, entry, meal, { live, doneMeals }) {
    const label = meal === "morning" ? "早宴席" : "晚宴席";
    const inviteKey = meal === "morning" ? "invite_morning" : "invite_evening";
    const info = live ? live.find((item) => item.meal === meal) : null;
    const serverState = info ? info.state : "unknown";
    const windowText = info ? info.window_text : "";
    const startText = info ? info.start_text : "";
    // 缺字段时按"从宽"处理：后端仍会把关，前端不替它下结论。
    const edible = info ? info.edible !== false : true;
    const invitable = info ? info.invitable === true : true;
    const eaten = doneMeals.has(meal);

    let stateKey;
    let note;
    if (eaten) {
      stateKey = "eaten";
      note = "本宴席日已吃过";
    } else if (serverState === "gap") {
      stateKey = "gap";
      note = "休整中 · 06:10 刷新";
    } else if (serverState === "makeup") {
      stateKey = "makeup";
      note = "已过邀请时间，只吃不邀请";
    } else if (serverState === "ready") {
      stateKey = "ready";
      note = windowText ? `开放中 ${windowText}` : "开放中";
    } else if (serverState === "not_started") {
      stateKey = "not_started";
      note = startText ? `${startText} 起开放` : "尚未到开放时间";
    } else if (serverState === "expired") {
      stateKey = "expired";
      note = "本顿已结束";
    } else {
      // 尚未联网读到时段：允许点（后端仍会按真实窗口把关），提示先刷新
      stateKey = "unknown";
      note = "点「刷新」获取开放时间";
    }

    const clickable =
      !eaten &&
      edible &&
      (stateKey === "ready" || stateKey === "makeup" || stateKey === "unknown");
    const canInvite = !eaten && clickable && invitable;

    // 邀请开关（该顿独立）—— 窗口外直接置灰，不给人"点了再说"的机会
    const toggle = el("input", { type: "checkbox", class: "toggle" });
    toggle.checked = entry.params[inviteKey] === true || entry.params[inviteKey] === "true";
    toggle.disabled = !canInvite;
    toggle.addEventListener("change", () => {
      entry.params[inviteKey] = toggle.checked;
      saveSelection();
    });

    const inviteLabel = el(
      "label",
      {
        class: `bbq-meal-invite${canInvite ? "" : " bbq-meal-invite--locked"}`,
        title: canInvite
          ? "这一顿邀请好友一起吃（花钻石）"
          : eaten
            ? "本宴席日已吃过"
            : "窗口外强制不邀请（后端也会拦）",
      },
      [toggle, el("span", { text: "邀请好友" })]
    );

    const cls = BBQ_MEAL_STATE_CLASS[stateKey] || BBQ_MEAL_STATE_CLASS.unknown;
    const button = el("button", {
      class: `btn btn--primary bbq-meal-btn ${cls.button}`,
      type: "button",
      // 补吃时在按钮上直接写明白，免得用户以为"这只和窗口内一样"
      text: stateKey === "makeup" ? `${label} · 补吃` : label,
      "data-task": task.name,
      "data-meal": meal,
      disabled: !clickable,
    });
    // 点了就按"这一顿 + 该顿邀请开关"提交一次运行
    button.addEventListener("click", () => onBbqMealClick(task, meal, button));

    return el("div", { class: `bbq-meal ${cls.panel}` }, [
      el("div", { class: "bbq-meal-top" }, [
        el("span", { class: "bbq-meal-name", text: label }),
        inviteLabel,
      ]),
      button,
      el("em", { class: "bbq-meal-note", text: note }),
    ]);
  }

  /**
   * 点击某一顿的按钮：把 ``meal`` 与该顿的邀请开关塞进参数，提交一次运行。
   *
   * 【为什么只提交宴席一个任务、不串活跃宝箱】
   * 这个按钮表达的是"吃这一顿"，语义要单一；带上宝箱会让用户以为
   * "吃宴席顺手做了一堆别的事"。与主动任务的默认行为（串宝箱）刻意不同。
   */
  async function onBbqMealClick(task, meal, button) {
    const entry = state.selection.get(task.name);
    if (!entry) return;
    // 参数说明：``meal`` 指定这一顿；``invite_morning/evening`` 已在 entry.params 里
    // （reconcileSelection 从规格表初始化过）。**不传 eat** —— 它不在规格表里，
    // 传了会被 normalize_params 丢弃并刷 WARNING；runner 对缺失的 eat 默认按
    // ``params.get("eat", True)`` 处理（网页路径 = 点了就真的吃）。
    const params = { ...entry.params, meal };
    try {
      const result = await submitRun(
        [{ name: task.name, params }],
        { reason: `宴席：${meal === "morning" ? "早宴席" : "晚宴席"}` }
      );
      if (result === "ok") {
        state.stopTasks.add(task.name);
        if (button) button.disabled = false;
        syncExecButtons();
      } else {
        appendRun("⚠", "宴席执行未提交成功，请稍后重试。", "warn");
      }
    } catch (error) {
      appendRun("✘", `宴席执行失败：${describeError(error)}`, "ERROR");
    }
  }

  /**
   * 联网读取宴席两顿的实时窗口状态（GET /api/banquet/status，只读、不发 15003）。
   */
  async function refreshBanquetStatus(button) {
    if (button) button.disabled = true;
    try {
      const payload = await call("/api/banquet/status");
      state.banquetMeals = payload.meals || [];
      if (payload.banquet) state.banquet = payload.banquet;
      renderTaskGroups();
      return payload;
    } catch (error) {
      appendRun("✘", `读取宴席时段失败：${describeError(error)}`, "ERROR");
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  /**
   * 主动任务（非宴席）的默认操作区：一个「执行」按钮。
   *
   * 宴席改用专用双按钮面板（见 :func:`bbqMealControls`），不走这里。
   */
  function manualExecControls(task) {
    const button = el("button", {
      class: "btn btn--primary task-exec",
      type: "button",
      text: execLabel(),
      "data-task": task.name,
    });
    // ★ 2026-10-04：执行中**不再禁用** —— 点了就排队（见 onExecClick）。
    button.disabled = !task.enabled;
    button.addEventListener("click", () => onExecClick(task, button));
    return [button];
  }

  // ------------------------------------------------------------------
  // 「执行」↔「停止执行」：同一个按钮的两种状态（2026-10-04 用户需求）
  // ------------------------------------------------------------------
  /**
   * 按钮常态文案（演练模式下带上模式名，与旧行为一致）。
   */
  function execLabel() {
    return isDevMode() ? `执行（${runModeText()}）` : "执行";
  }

  /**
   * 点击「执行 / 停止执行」的统一入口。
   *
   * 【为什么执行期间不禁用按钮（2026-10-04 用户需求）】
   * 上一版是"运行中禁用所有执行按钮 + 点了弹窗说上一个还没跑完"。
   * 用户要的是：**直接挂在后台排队，按顺序全部跑完** —— 于是禁用法必须去掉，
   * 否则"排队"这个动作根本点不出来。
   *
   * 【为什么只有被点过的那张卡片变成「停止执行」】
   * 停止的是"我刚点的这件事"。其它卡片的按钮保持「执行」，用户想再排一个
   * 才有的点。全部变成停止，就等于"想排队却只能停"。
   */
  function onExecClick(task, button) {
    if (state.stopTasks.has(task.name)) {
      stopCurrentRun(task);
      return;
    }
    if (task.group === "manual") runManualTask(task, button);
    else runSingleAutoTask(task, button);
  }

  /**
   * 让界面上所有执行按钮的**文案与配色**跟状态对齐（不重建 DOM）。
   *
   * 【为什么只改属性而不 renderTaskGroups()】
   * 轮询 1.2 秒一次；整组重建会打断正在填的参数、把滚动位置弹回顶部。
   * 按钮只有文字与一个类名会变，原地改最省事也更稳。
   */
  function syncExecButtons() {
    for (const button of document.querySelectorAll(".task-exec, .task-auto-exec")) {
      const name = button.getAttribute("data-task");
      if (!name) continue;
      const stopping = state.stopTasks.has(name);
      button.classList.toggle("task-stop", stopping);
      button.textContent = stopping ? "停止执行" : execLabel();
      button.title = stopping ? "中断正在执行的任务" : button.title;
    }
  }

  /**
   * 中断**当前正在跑的那一批**（队列里剩下的照常跑）。
   *
   * 【为什么成功后要往运行结果里写一行】
   * 服务端的日志要经过轮询才会到界面上，中间有 1 秒左右的空窗。
   * 用户点了停止却"什么都没发生"，第一反应一定是"按钮坏了" ——
   * 所以这里立刻补一行本地结果，点下去就有反馈。
   */
  async function stopCurrentRun(task) {
    try {
      const payload = await call("/api/stop", { method: "POST" });
      if (payload && payload.stopped) {
        appendRun("⏹", `已请求中断：${task.title}`, "warn");
      } else {
        appendRun("⚠", "当前没有可中断的运行。", "warn");
        state.stopTasks.delete(task.name);
        syncExecButtons();
      }
    } catch (error) {
      appendRun("✘", `中断失败：${describeError(error)}`, "ERROR");
    }
  }

  /**
   * 活动扫荡展开栏顶部的工具行：「更新区域」按钮（2026-10-03 从标题行移入，
   * 并删掉"下次自动更新"等状态说明 —— 数据新不新，看下拉里的选项即可）。
   */
  function activityRefreshTools() {
    const button = el("button", {
      class: "btn btn--ghost task-refresh",
      type: "button",
      text: "更新区域",
    });
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      await refreshActivity(button);
    });
    return el("div", { class: "task-tools" }, [button]);
  }

  /**
   * 刷新主线最高通关与下一待推关卡进度，并联动更新界面所有关联区域（顶部卡片、任务工具、参数横幅）。
   */
  async function refreshMainStageStatus() {
    try {
      const res = await call("/api/main_stage/status");
      state.mainStageStatus = res;
      renderTaskGroups();
      renderInfo();
      return res;
    } catch (e) {
      console.warn("自动刷新主线进度失败", e);
      return null;
    }
  }

  /**
   * 主线推图展开栏顶部的进度横幅 + 「刷新进度」按钮（2026-10-03 从标题行移入，
   * 删掉自动推进之类的行为说明文字）。
   */
  function renderMainStageBanner() {
    const status = state.mainStageStatus || (state.player && state.player.main_stage);
    const highest =
      status && status.highest_passed_desc ? status.highest_passed_desc : "尚未读取";
    const next = status && status.next_section_desc ? status.next_section_desc : "—";
    const button = el("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "刷新进度",
    });
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "刷新中…";
      try {
        await refreshMainStageStatus();
      } catch (e) {
        showToast(`刷新主线进度失败：${describeError(e)}`);
      } finally {
        button.disabled = false;
        button.textContent = "刷新进度";
      }
    });
    return el("div", { class: "param" }, [
      el("div", { class: "param-tools" }, [
        el("img", { class: "star-icon-img", src: "assets/icons/ui/star_gold.png", alt: "", "aria-hidden": "true" }),
        el("span", { text: "当前已通关最高主线" }),
        el("span", { class: "badge badge--warn", text: highest }),
        button,
      ]),
      el("p", { class: "param-hint", text: `下一待推关卡：${next}` }),
    ]);
  }

  // ------------------------------------------------------------------
  // 活动关卡候选（★ 2026-09-22 新增）
  // ------------------------------------------------------------------
  // 【这一块解决的问题】
  // 「降临 / 活动关卡扫荡」的活动区域与指定关卡原本只能手输 ID ——
  // 因为后端虽然把清单拉回来了（11025 / 11003），却只写进日志、
  // TaskResult.data 里只留了数量，界面层拿不到任何结构化数据。
  // 现在后端把它们原样端出来（GET /api/activity/preview），这里负责渲染成下拉。
  //
  // 【为什么缓存放在 state 里而不是 DOM 里】
  // 任务卡片会被整体重建（勾一次任务、改一次参数都会重渲染），DOM 里的
  // <option> 会一起消失。缓存放在 state，重建后能立刻把上次拉到的候选填回去 ——
  // 否则用户每点一次勾选框，下拉就变空，得像"刷新"一样再拉一次。

  /** 取某个来源已拉到的候选（没拉过时返回空数组）。 */
  function activityOptions(source) {
    return state.activityOptions[source] || [];
  }

  /** 区域候选项的文字：`可疑的工作（进度 5/5）`。不展示活动 ID 和持续时间，保留进度。 */
  function areaOptionLabel(area) {
    const progress = area.progress ? `进度 ${area.progress}` : "进度 ?";
    return `${area.name || "<无名>"}（${progress}）`;
  }

  /**
   * 关卡候选项的文字：`关卡9（体力 8｜已通关）`。
   *
   * 【★ 2026-10-05 去掉 section_id】与 ``areaOptionLabel`` /
   * ``materialAreaOptionLabel`` / ``materialSectionOptionLabel`` 统一口径：
   * 展示层**只给人话**，那一串 9 位数字对用户没有任何意义。
   * 下拉的 ``value`` 仍然取自 ``item.section_id``（见 ``renderCandidateParam``），
   * 提交给后端的还是真 ID —— 显示文案与取值完全解耦，这里是纯展示改动。
   */
  function sectionOptionLabel(section) {
    const statusText = section.passed
      ? "已通关"
      : section.needs_first_pass
      ? "未通关-可首通"
      : "未通关";
    return `${section.name || "<无名>"}（体力 ${section.energy_cost}｜${statusText}）`;
  }

  /** 素材区域候选项的文字：`[角色素材] 戰士素材 (战士)` */
  function materialAreaOptionLabel(area) {
    return `[${area.category_name || "素材"}] ${area.name || ""}`;
  }

  /** 素材关卡候选项的文字：`戰士素材 初級（1级）` */
  function materialSectionOptionLabel(section) {
    const lv = section.level_limit ? `（${section.level_limit}级）` : "";
    return `${section.name || ""}${lv}`;
  }

  /**
   * 确保素材与试炼关卡静态目录树已载入（GET /api/material/tree）。
   * 属于客户端固定静态表配置，零网络发包，拉取一次后常驻 state.materialTree。
   */
  async function ensureMaterialTree() {
    if (state.materialTree) return state.materialTree;
    try {
      const payload = await call("/api/material/tree", { params: { category: "all" } });
      state.materialTree = payload;
      state.materialError = "";
      return payload;
    } catch (error) {
      state.materialError = describeError(error);
      return null;
    }
  }

  /**
   * 统一获取候选选项（涵盖活动关卡动态候选与素材关卡静态候选）。
   */
  function candidateOptions(entry, source) {
    if (source === "activity_areas") {
      return activityOptions(source);
    }
    if (source === "activity_sections") {
      const areaVal = entry && entry.params && entry.params.area ? String(entry.params.area).trim() : currentAreaValue();
      if (areaVal && state.activityAllSections && state.activityAllSections[areaVal]) {
        return state.activityAllSections[areaVal].sweepable || [];
      }
      return activityOptions(source);
    }
    if (source === "material_areas") {
      if (!state.materialTree || !Array.isArray(state.materialTree.areas)) return [];
      const cat = entry && entry.params && entry.params.category ? entry.params.category : "all";
      if (cat === "all") return state.materialTree.areas;
      return state.materialTree.areas.filter((a) => a.category === cat);
    }
    if (source === "material_sections") {
      if (!state.materialTree || !Array.isArray(state.materialTree.areas)) return [];
      const areaVal = entry && entry.params && entry.params.area ? String(entry.params.area).trim() : "";
      if (!areaVal) return [];
      const area = state.materialTree.areas.find(
        (a) => String(a.area_id) === areaVal || String(a.display_id) === areaVal
      );
      return area && Array.isArray(area.sections) ? area.sections : [];
    }
    return [];
  }

  /**
   * 调 `/api/activity/preview` 取候选（**只读**：后端用 `times=0`，绝不会发 11009）。
   *
   * @param area 给了就是"该区的关卡候选"，不给就是"当期区域清单"。
   * @param refresh true = 跳过缓存强制重拉（「更新区域」按钮用）。
   */
  async function fetchActivity({ area = null, refresh = false } = {}) {
    const params = {};
    if (area) params.area = String(area);
    if (refresh) params.refresh = 1;
    return call("/api/activity/preview", { params });
  }

  /**
   * 把后端响应写进 state（候选 + 缓存元信息 + 错误）。
   *
   * 【为什么要单独一个函数】
   * 有四处会拿到同样的响应（启动自动拉、勾选补拉、按钮刷新、换区域拉关卡）。
   * 每处各写一遍 state 赋值，迟早会出现"某处忘了清错误"这类半新半旧的状态。
   */
  function applyActivityPayload(payload) {
    if (!payload) return;
    if (Array.isArray(payload.areas) && payload.areas.length) {
      state.activityOptions.activity_areas = payload.areas;
    }
    if (payload.all_sections && typeof payload.all_sections === "object") {
      state.activityAllSections = payload.all_sections;
    }
    const curArea = payload.area_selector || currentAreaValue();
    if (curArea && state.activityAllSections && state.activityAllSections[curArea]) {
      state.activityOptions.activity_sections = state.activityAllSections[curArea].sweepable || [];
      state.activityBlocked = state.activityAllSections[curArea].blocked || [];
    } else if (payload.area_selector && payload.sections) {
      state.activityOptions.activity_sections = payload.sections.sweepable || [];
      state.activityBlocked = payload.sections.blocked || [];
    }
    if (payload.cache) state.activityCache = payload.cache;
    state.activitySummary = payload.summary || "";
    state.activityError = (payload.errors && payload.errors[0]) || "";
  }

  /**
   * 确保候选可用：**命中缓存时一个包都不发**，过期时才联网拉一次。
   *
   * 【为什么没登录就直接返回】
   * 区域表要带会话令牌（11025 需要登录）。没登录就在这里发起，只会拿到一句
   * "需要先登录"的错误，反而把状态行染红 —— 不如让状态行如实写"需要先登录"，
   * 用户登录后 `loadTasks()` 会再调一次本函数。
   */
  async function ensureActivityCache({ refresh = false } = {}) {
    const session = state.session || {};
    if (!session.has_session) return null;
    try {
      const payload = await fetchActivity({ refresh });
      applyActivityPayload(payload);
      // 已经选过区域（本地存的）→ 顺手把该区关卡也备好（命中缓存则零请求）
      const areaValue = currentAreaValue();
      if (areaValue && !state.activityError) {
        applyActivityPayload(await fetchActivity({ area: areaValue }));
        clearStaleSectionSelections();
      }
      return payload;
    } catch (error) {
      state.activityError = describeError(error);
      return null;
    }
  }

  /** 当前表单里选中的活动区域（没选就是空串）。 */
  function currentAreaValue() {
    for (const [name, entry] of state.selection.entries()) {
      const view = state.views.get(name);
      if (!view) continue;
      const param = (view.params || []).find(
        (item) => item.options_source === "activity_areas"
      );
      if (param && entry.params[param.name]) return String(entry.params[param.name]);
    }
    return "";
  }

  /** 下拉里若还存着"当前候选里没有"的关卡（换期 / 换区后被挡），就清掉它。 */
  function clearStaleSectionSelections() {
    let changed = false;
    for (const [name, entry] of state.selection.entries()) {
      const view = state.views.get(name);
      if (!view) continue;
      const param = (view.params || []).find(
        (item) => item.options_source === "activity_sections"
      );
      if (!param) continue;
      const chosen = entry.params[param.name];
      if (!chosen) continue;
      const available = activityOptions("activity_sections").some(
        (item) => String(item.section_id) === String(chosen)
      );
      if (!available) {
        entry.params[param.name] = "";
        changed = true;
      }
    }
    if (changed) saveSelection();
  }

  /** 换区域后自动拉该区的关卡候选（命中缓存则零请求）。 */
  async function loadSectionsForArea(areaValue) {
    if (!areaValue) return;
    try {
      applyActivityPayload(await fetchActivity({ area: String(areaValue) }));
      clearStaleSectionSelections();
    } catch (error) {
      state.activityError = describeError(error);
    }
  }

  /**
   * 用户点「更新区域」：**强制**重拉区域清单，并把当前区的关卡也一起刷新。
   *
   * 【为什么连关卡一起刷】
   * "更新区域"的语义是"游戏换期了，重新对齐一次"。只刷区域、不刷关卡的话，
   * 界面上会出现"新区域 + 旧关卡号"的组合，而这个组合在游戏里根本不存在。
   */
  async function refreshActivity(button) {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "更新中…";
    state.activityError = "";
    try {
      applyActivityPayload(await fetchActivity({ refresh: true }));
      const areaValue = currentAreaValue();
      if (areaValue && !state.activityError) {
        applyActivityPayload(await fetchActivity({ area: areaValue }));
      }
      clearStaleSectionSelections();
    } catch (error) {
      state.activityError = describeError(error);
    } finally {
      button.disabled = false;
      button.textContent = label;
      renderTaskGroups();
    }
  }

  /**
   * 一行参数：标签 + 若干控件（危险参数整行标红，与原有样式一致）。 */
  function paramRow(param, ...controls) {
    return el("label", { class: `param ${param.danger ? "param--danger" : ""}` }, [
      el("span", { text: param.label }),
      ...controls,
    ]);
  }

  // ------------------------------------------------------------------
  // 自定义下拉 —— **已上移到 common.js（2026-10-05）**
  // ------------------------------------------------------------------
  // 它原本只服务本页的参数区。但登录页的"已保存的账号"同样是原生 <select>，
  // 在 Android WebView 上会弹出那个换行重叠、完全没法读的系统弹框 ——
  // 两页都要用，于是挪进两页都加载的公共库，这里直接用 window.Ct.customSelect。
  //
  // 原始设计说明（保留在此，便于读到这段的人理解它为何存在）：
  // 手机上浏览器会**接管**原生 select，从屏幕底部弹出系统滚轮 / 全屏单选弹窗
  // （iOS 的 Picker、Android 的 Modal Sheet）—— 它与本应用的暗黑一体化界面完全
  // 割裂，还会盖住参数区的上下文。改成自绘下拉之后，PC 与手机是**同一套**交互：
  // 点一下在按钮下方原地展开，点选项即生效，点别处 / 按 Esc 收起。
  //
  // 样式仍在 ``web/assets/input.css`` 的 ``.custom-select*``，
  // 改动后需要重新构建 CSS：``npm run build:css``。

  /**
   * 数字步进器（★ 2026-10-02 需求）：**左减号 / 中间可手输 / 右加号**。
   *
   * 【为什么要做它】
   * 手机上的 ``<input type="number">`` 需要在 6 英寸屏上点中那个 12px 的上下箭头，
   * 而且浏览器自带的 spinners 在各家 Android 上外观差异极大；"买几次 / 打几场"
   * 这类值又只会在 0~10 之间来回调 —— 加减号比键盘更好按。
   * 中间仍然保留真正的 ``<input type="number">``，所以**键盘手输没有被拿掉**：
   * 大数值（例如钻石上限 99999）照样能一次输入。
   *
   * .. important::
   *    扫荡次数**不适用**本控件 —— 它带 ``choices``（1 / 5 / 10 三档），
   *    在 :func:`renderParam` 里会先走"档位下拉"分支。理由见
   *    ``models/activity_stage.SWEEP_TIME_CHOICES``：客户端面板只有三档，
   *    给自由数字等于让用户填一个会被后端悄悄归位的值。
   *
   * :param entry: 任务选择项（它的 ``params`` 会被就地更新）。
   * :param param: 参数规格（``kind`` 是 int / float）。
   * :return: ``.stepper`` 容器（减号 + 输入框 + 加号）。
   */
  function numberStepper(entry, param) {
    const isFloat = param.kind === "float";
    const step = isFloat ? 0.5 : 1;
    const current = entry.params[param.name];

    const input = el("input", {
      type: "number",
      min: "0",
      step: String(step),
      inputmode: isFloat ? "decimal" : "numeric",
      class: "stepper-input",
    });
    input.value = current === null || current === undefined ? "" : String(current);

    /** 把"算好的数值"写回参数并持久化（数字用数字存，后端会照 kind 再转一次）。 */
    function commit(value) {
      input.value = String(value);
      entry.params[param.name] = value;
      saveSelection();
      renderDiamondList();
    }

    /** 按步长加减：空值 / 非法值按 default → 0 处理，绝不产生负数。 */
    function nudge(direction) {
      const typed = Number(input.value);
      const fallback = Number(param.default);
      const base = Number.isFinite(typed)
        ? typed
        : Number.isFinite(fallback)
          ? fallback
          : 0;
      const raw = base + direction * step;
      const clamped = Math.max(0, raw);
      commit(isFloat ? Math.round(clamped * 100) / 100 : Math.round(clamped));
    }

    input.addEventListener("input", () => {
      // 输入过程中不纠正（否则删到一半就被改成 0，没法输入新数字）
      entry.params[param.name] = input.value;
      saveSelection();
    });
    input.addEventListener("blur", () => {
      const raw = String(input.value).trim();
      if (raw === "") {
        // 空 = "没填"（例如荣耀之巅的 battles 空着就是只读），保持空
        entry.params[param.name] = "";
        saveSelection();
        return;
      }
      let value = Number(raw);
      if (!Number.isFinite(value) || value < 0) value = 0;
      // 浮点按步长对齐（1.3 → 1.5），整数四舍五入（3.7 → 4）
      value = isFloat
        ? Math.round(value / step) * step
        : Math.round(value);
      commit(isFloat ? Math.round(value * 100) / 100 : value);
    });

    const minus = el("button", {
      type: "button",
      class: "stepper-btn stepper-btn--minus",
      text: "−",
      "aria-label": `${param.label} 减 ${step}`,
    });
    const plus = el("button", {
      type: "button",
      class: "stepper-btn stepper-btn--plus",
      text: "＋",
      "aria-label": `${param.label} 加 ${step}`,
    });
    minus.addEventListener("click", () => nudge(-1));
    plus.addEventListener("click", () => nudge(1));

    return el("div", { class: "stepper" }, [minus, input, plus]);
  }

  /**
   * 档位选项的显示文案（如扫荡次数 `0 / 1 / 5 / 10`）。
   *
   * 【为什么不让用户自由填数字】
   * 真实客户端的扫荡面板只有 1 / 5 / 10 三档（`StageModule.eSweepType`）。
   * 自由输入框既让人不知道该填几，填了也会被后端 `normalize_sweep_times`
   * 悄悄向下归位 —— 界面直接给档位才是"所见即所发"。
   */
  function choiceOptionText(param, choice) {
    // 后端可为某个档位值指定文案（例如 -1 在活动关是"扫荡到清空体力"、
    // 在素材关是"扫荡到次数耗尽"）—— 有就以它为准，别让前端按 -1 猜。
    const labels = param.choice_labels || {};
    if (Object.prototype.hasOwnProperty.call(labels, String(choice))) {
      return labels[String(choice)];
    }
    if (param.name === "category") {
      if (choice === "all") return "全部素材 (all)";
      if (choice === "job") return "角色素材 (job)";
      if (choice === "element") return "元素试炼 (element)";
      return String(choice);
    }
    if (typeof choice === "number") {
      return choice === 0 ? "0（只读：什么都不扫）" : `${choice} 次`;
    }
    return String(choice);
  }

  function renderChoiceParam(entry, param) {
    const current = entry.params[param.name];
    // 存着的旧值不在档位里（例如以前手填的 7）→ 归到第一档（0 = 只读）。
    // 方向与后端 `normalize_sweep_times` 一致：宁可少扫，绝不多扫。
    const wanted = current === null || current === undefined ? "" : String(current);
    const matched = param.choices.some((choice) => String(choice) === wanted);
    const selected = matched ? wanted : String(param.choices[0]);
    entry.params[param.name] = selected;

    const control = customSelect({
      value: selected,
      options: param.choices.map((choice) => ({
        value: String(choice),
        label: choiceOptionText(param, choice),
      })),
      onChange: (value) => {
        entry.params[param.name] = value;
        saveSelection();
        if (param.name === "category") {
          // 切换素材大类：若当前已选区域不属于新大类，清空 area 与 section
          const newCat = value;
          const currentAreaId = entry.params.area;
          if (currentAreaId && state.materialTree && Array.isArray(state.materialTree.areas)) {
            const areaObj = state.materialTree.areas.find(
              (a) => String(a.area_id) === String(currentAreaId) || String(a.display_id) === String(currentAreaId)
            );
            if (areaObj && newCat !== "all" && areaObj.category !== newCat) {
              entry.params.area = "";
              entry.params.section = "";
              saveSelection();
            }
          }
          renderTaskGroups();
        }
        renderDiamondList();
      },
    });
    return paramRow(param, control, el("em", { class: "param-hint", text: param.hint }));
  }

  /**
   * 候选参数（活动区域 / 指定关卡 / 素材区域 / 素材指定关卡）：**只有下拉，不提供手输**。
   *
   * 【为什么删掉手输框（2026-09-22 用户要求）】
   * 区域号与关卡号每期都换，让用户去别处抄数字，抄错的表现是"任务跑完什么都没发生"
   * （服务端按不存在的 ID 直接拒绝），排查成本极高。所以这里只给候选；
   * 没有候选时**如实说明原因**（未登录 / 还没拉到），而不是给一个看似能用的输入框。
   *
   * 【刷新入口只有一个】
   * 展开栏顶部的「更新区域」（2026-10-03 从标题行移入）。参数区里不再放
   * 其他刷新按钮，避免"两个按钮各刷一半"的混乱（那个只刷区域、不刷关卡）。
   */
  function renderCandidateParam(entry, param) {
    const source = param.options_source;
    const current = entry.params[param.name];
    const known = candidateOptions(entry, source);

    /** 下拉里的一项（``disabled`` 的选项只是"说明文字"，点不动）。 */
    const options = [];
    let selectDisabled = false;

    if (known.length === 0) {
      let emptyText = "（尚无候选）";
      if (source === "activity_areas") {
        emptyText = "（尚无候选：请先登录并点「更新区域」）";
      } else if (source === "activity_sections") {
        emptyText = "（请先选择活动区域）";
      } else if (source === "material_areas") {
        emptyText = state.materialError
          ? `（加载素材目录失败：${state.materialError}）`
          : "（正在加载素材目录…）";
      } else if (source === "material_sections") {
        emptyText = entry.params.area ? "（该区域无可用关卡）" : "（请先选择目标区域）";
      }
      options.push({ value: "", label: emptyText, disabled: true });
      selectDisabled = true;
    } else {
      let defaultText = "（不指定）";
      if (source === "activity_areas") {
        defaultText = "（请选择活动区域）";
      } else if (source === "activity_sections") {
        defaultText = "（不指定 = 扫该区全部可扫关卡）";
      } else if (source === "material_areas") {
        defaultText = "（不指定 = 仅展示分类目录）";
      } else if (source === "material_sections") {
        defaultText = "（不指定 = 扫该区最高难度）";
      }
      options.push({ value: "", label: defaultText });

      for (const item of known) {
        let val = "";
        let label = "";
        if (source === "activity_areas") {
          val = String(item.area_id);
          label = areaOptionLabel(item);
        } else if (source === "activity_sections") {
          val = String(item.section_id);
          label = sectionOptionLabel(item);
        } else if (source === "material_areas") {
          val = String(item.area_id);
          label = materialAreaOptionLabel(item);
        } else if (source === "material_sections") {
          val = String(item.section_id);
          label = materialSectionOptionLabel(item);
        }
        options.push({ value: val, label });
      }
    }

    // 参数里存的就是 areaID / sectionID（与命令行同一套取值，后端只认这一条路径）。
    const wanted = current === null || current === undefined ? "" : String(current);
    const matched = options.some((option) => option.value === wanted);
    let selected = wanted;
    if (wanted && !matched) {
      // 存着旧值但候选里没有（换期了 / 之前是手填的）→ 清空，
      // 免得带着一个失效 ID 去扫（服务端只会回一句含糊的拒绝）。
      entry.params[param.name] = "";
      selected = "";
    }

    const control = customSelect({
      value: selected,
      options,
      disabled: selectDisabled,
      emptyText: options.length > 0 ? options[0].label : "（尚无候选）",
      onChange: (value) => {
        entry.params[param.name] = value;
        saveSelection();
        if (source === "activity_areas") {
          // 换区域：若全量关卡已在前端预加载（all_sections），直接同步就绪，零延迟刷新关卡下拉
          if (state.activityAllSections && state.activityAllSections[value]) {
            state.activityOptions.activity_sections = state.activityAllSections[value].sweepable || [];
            state.activityBlocked = state.activityAllSections[value].blocked || [];
            clearStaleSectionSelections();
            renderTaskGroups();
          } else {
            state.activityOptions.activity_sections = [];
            state.activityBlocked = [];
            clearStaleSectionSelections();
            renderTaskGroups();
            // 异步兜底拉关卡
            void loadSectionsForArea(value).then(() => renderTaskGroups());
          }
        } else if (source === "material_areas") {
          // 换素材区域 → 清空上一区的指定关卡，并重新渲染刷新关卡下拉列表
          entry.params.section = "";
          saveSelection();
          renderTaskGroups();
        }
        renderDiamondList();
      },
    });

    let hint = param.hint;
    if (source === "activity_areas") {
      hint = "当期开放的活动区域";
    } else if (source === "activity_sections") {
      hint = "该区域可扫荡的关卡";
    } else if (source === "material_areas") {
      hint = "选择目标素材区域（不指定则列出目录）";
    } else if (source === "material_sections") {
      hint = "指定难度关卡（不指定则默认扫最高难度）";
    }
    return paramRow(
      param,
      control,
      el("em", { class: "param-hint", text: hint })
    );
  }

  function renderParam(entry, param) {
    const current = entry.params[param.name];
    let input;

    // ★ 这两个分支必须排在 ``kind`` 判断**之前**：``choices`` / ``options_source``
    // 描述的是"怎么渲染"，而 ``kind`` 只说值是什么类型（times 是 int，界面只给三档）。
    if (Array.isArray(param.choices) && param.choices.length > 0) {
      return renderChoiceParam(entry, param);
    }
    if (param.options_source) {
      return renderCandidateParam(entry, param);
    }

    // ★ 2026-10-02：数字项一律渲染成**加减号步进器**（左 [-] / 中间手输 / 右 [+]），
    // 而不是裸的 number 输入框：手机上 12px 的上下箭头几乎点不中，而这类值
    // 通常只在 0~10 之间来回调。注意**扫荡次数不在这里** —— 它带 choices，
    // 上面第一个分支就已经把它渲染成 1 / 5 / 10 三档下拉了。
    if (param.kind === "int" || param.kind === "float") {
      return el(
        "label",
        { class: `param ${param.danger ? "param--danger" : ""}` },
        [
          el("span", { text: param.label }),
          numberStepper(entry, param),
          el("em", { class: "param-hint", text: param.hint }),
        ]
      );
    }

    if (param.kind === "bool") {
      // bool 参数也是拨动开关（2026-10-01）：行右侧，说明文字换行铺满
      input = el("input", { type: "checkbox", class: "toggle" });
      input.checked = current === true || current === "true";
      input.addEventListener("change", () => {
        entry.params[param.name] = input.checked;
        saveSelection();
        renderDiamondList();
      });
    } else {
      input = el("input", { type: "text", autocomplete: "off" });
      input.placeholder = param.hint;
      input.value = current === null || current === undefined ? "" : String(current);
      input.addEventListener("input", () => {
        entry.params[param.name] = input.value;
        saveSelection();
      });
    }

    return el(
      "label",
      {
        class: `param ${param.kind === "bool" ? "param--bool" : ""} ${
          param.danger ? "param--danger" : ""
        }`,
      },
      [
        el("span", { text: param.label }),
        input,
        el("em", { class: "param-hint", text: param.hint }),
      ]
    );
  }

  // ------------------------------------------------------------------
  // 组装请求（★ 2026-10-02：主动任务的"二次确认"已按需求取消）
  // ------------------------------------------------------------------
  /**
   * 收集"要提交的任务"。
   *
   * :param group: 只收集某个分组的任务（``"auto"`` = 自动任务）。``null`` = 全部。
   *
   * 【为什么现在要按分组收集（2026-09-24）】
   * 提交场景从一个变成了两种：
   * - 自动执行：只提交 auto 组的勾选项（见 runAutomation）；
   * - 主动任务：只提交被点开确认的那一个（见 runManualTask，它自己拼任务数组）。
   * 而且"勾选"这个标记现在**只对自动任务有意义**（主动任务没有复选框了），
   * 按分组过滤比在每个调用点写 if 更不容易出错。
   */
  function collectTasks({ group = null } = {}) {
    const tasks = [];
    for (const task of state.tasks) {
      if (!task.enabled || task.hidden) continue; // 占位项与"由专用界面负责"的任务不提交
      if (group && task.group !== group) continue;
      const entry = state.selection.get(task.name);
      if (!entry || !entry.checked) continue;
      // 参数按"声明了什么就传什么"提交；服务端会做类型转换与校验
      tasks.push({ name: task.name, params: { ...entry.params } });
    }
    return tasks;
  }

  /**
   * 运行级参数（与具体任务无关）：演练、总闸、详细日志。
   *
   * ★ 2026-09-25：``battles`` / ``interval`` 已从这里删除 —— 它们是荣耀之巅的
   * **任务级参数**，随那张卡片的 ``params`` 一起提交（见 collectTasks /
   * runManualTask）。参数归属的唯一来源是后端的规格表（``/api/tasks``），
   * 前端不再自己猜"哪个任务需要哪个参数"。
   *
   * 【为什么把它从原来的 buildPayload 里拆出来】
   * 原来 buildPayload 把"任务清单 + 运行选项"拼成一个 body，只服务一个入口
   * （底部的一键运行）。现在提交有两处（自动执行 / 主动任务），把"运行选项"
   * 独立出来、任务清单各自拼，就不会出现"某个入口漏传了演练开关"这种最危险的
   * bug —— 本来只想演练，结果真发包。
   */
  function runOptions() {
    return {
      dry_run: isDryRun(),
      allow_diamond: Boolean($("opt-allow-diamond") && $("opt-allow-diamond").checked),
      // ★ 2026-10-05：true = 勾选框已从服务端还原过，allow_diamond 是用户
      // 此刻的明确选择，后端照单全收；false（旧 JS / 还原失败）时后端改用
      // 「已保存值 或 请求值」合并 —— 详见 webapi/app.py 的 start_run。
      allow_diamond_synced: Boolean(state.allowDiamondSynced),
      verbose: Boolean($("opt-verbose") && $("opt-verbose").checked),
    };
  }

  /**
   * ★ 2026-10-02：原来的 ``dangerReasons()``（"会消耗资源"清单，供二次确认使用）
   * 已按需求**整体删除**。原因：
   *
   * 1. 主动任务的二次确认弹窗已取消（见 ``runManualTask``），它失去了唯一调用点；
   * 2. 留着它会造出一个"看似还在把关"的假象 —— 一个没人调用的安全检查，
   *    比没有检查更危险（维护者会以为风险已经被兜住了）。
   *
   * "会消耗资源"这件事现在只由**卡片徽标**（``taskBadges`` 的「有消耗」）与
   * 参数的「⚠ 消耗资源」标注来表达，那是**展示**，不是一道会被绕过的校验。
   */

  /** 任务名 → 中文标题（清单还没加载时退回任务名）。 */
  function titleOf(name) {
    return (state.views.get(name) || {}).title || name;
  }

  // ------------------------------------------------------------------
  // 自动任务：勾选即生效的"自动执行"（★ 2026-09-24 用户要求）
  //
  // 【触发时机】
  // "每次启动脚本"与"状态变动"各触发一次：
  //   · 启动：进入主界面（boot 时已有会话 / 登录成功 / 切换区服成功）；
  //   · 状态变动：自动任务的勾选被改动（见 autoCheckbox）。
  // 运行中触发**不排队**（硬提交只会撞服务端的 409），只记 pendingAuto，
  // 等这一次跑完再补（见 pollOnce）。
  //
  // 【去重：同一个游戏日只跑一次】
  // game_day 由后端按"游戏服务器每天 05:00 换日"算好（/api/tasks 的字段）。
  // 记录里同时带上 server_id / player_id —— 换号或换区视为**新的对象**，
  // 必须重跑一次，否则新号一整天都不做日常。
  //
  // 【★ 2026-10-06：后端 once_per_day 那批只在"第一次登录工具时"自动执行】
  // 礼包 / 工会签到 / 挖矿 / 每日免费抽卡 / 荣耀之巅宝箱这几张卡，
  // 自动执行**只跟开机那一下走**（启动 / 登录成功 / 切区 / 启动撞 409 后的补跑）；
  // "勾选变动"这条路径会把它们摘掉（见 ONCE_PER_DAY_TRIGGERS）——
  // 当天第一次打开时没勾上、之后随手勾上，不该在那一刻就跑掉。
  // ⚠️ 卡片上的「执行」小按钮是**手动路径**（runSingleAutoTask，origin="manual"），
  // 不受这条限制，任何时候点都照跑 —— 主动执行优先级最高。
  //
  // 【唯一的例外：活跃宝箱】
  // 它跟着主动任务补领（见 runManualTask），所以同一天可以跑多次。
  // ------------------------------------------------------------------
  function readAutoRunRecord() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.autoRun);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null; // 存坏了就当作"没跑过"：宁可多跑一次，也绝不因为一条烂记录永远不跑
    }
  }

  function saveAutoRunRecord(record) {
    try {
      localStorage.setItem(STORAGE_KEYS.autoRun, JSON.stringify(record));
    } catch {
      /* 隐私模式 / 存储被禁用：只影响"今天是否判重"，不影响这次执行 */
    }
  }

  /**
   * 这个任务在**当前游戏日、当前账号区服**下是否已经自动跑过。
   *
   * 【为什么两个来源取并集（2026-10-04）】
   * 判重原本只靠 localStorage：它按 origin 隔离，换端口、换窗口（桌面壳
   * 与 Edge 回落走的是两套存储）都会丢，于是"同一天第二次登录"又跑一遍。
   * 服务端那份（``state.autoDone``）才是权威 —— 但旧用户本地可能已有记录，
   * 所以取**并集**：任一命中即跳过。宁可少跑一遍（还有「执行」小按钮可手动补），
   * 也不在同一天反复打扰。
   *
   * 【为什么三个条件都要对得上（localStorage 那一份）】
   * 只比任务名不够：换了号或换了区之后任务名一模一样，但那个号从没做过日常。
   */
  function ranToday(name) {
    if (state.autoDone.has(name)) return true;
    const record = readAutoRunRecord();
    if (!record || record.game_day !== state.gameDay) return false;
    const session = state.session || {};
    if (String(record.server_id ?? "") !== String(session.server_id ?? "")) return false;
    if (String(record.player_id ?? "") !== String(session.player_id ?? "")) return false;
    return Array.isArray(record.tasks) && record.tasks.includes(name);
  }

  /**
   * 这个任务是否属于「每游戏日只自动跑一次」（后端 ``once_per_day`` 名单）。
   *
   * 【名单的唯一来源是后端】
   * ``/api/tasks`` 每条记录都带 ``once_per_day``（派生自 ``services/task_spec.py``
   * 的规格表）。前端**不自己抄一份** —— 抄一份的话，后端加了一个任务却忘了在
   * 前端登记，那个新任务就会在"勾选变动"路径上被漏掉过滤，表现是"偶尔多跑一次"，
   * 极难定位。
   */
  function isOncePerDay(name) {
    return Boolean((state.views.get(name) || {}).once_per_day);
  }

  /** 记下"这些任务在本游戏日已经跑过"。 */
  function markAutoRun(names) {
    const session = state.session || {};
    const previous = readAutoRunRecord();
    const sameTarget =
      previous &&
      previous.game_day === state.gameDay &&
      String(previous.server_id ?? "") === String(session.server_id ?? "") &&
      String(previous.player_id ?? "") === String(session.player_id ?? "");
    const tasks = sameTarget && Array.isArray(previous.tasks) ? previous.tasks : [];
    saveAutoRunRecord({
      game_day: state.gameDay,
      server_id: session.server_id ?? null,
      player_id: session.player_id ?? null,
      tasks: Array.from(new Set([...tasks, ...names])),
      at: new Date().toLocaleTimeString("zh-CN"),
    });
  }

  /**
   * 「自动任务」面板顶部的状态行（替代已删除的底部操作条）。
   *
   * 它回答三个用户真正关心的问题：**今天还会不会跑、跑过哪些、现在什么模式**。
   */
  function renderAutoStatus() {
    const node = $("auto-status");
    if (!node) return;
    node.className = "auto-status";

    const session = state.session || {};
    if (!session.has_session) {
      node.classList.add("auto-status--warn");
      node.textContent = "自动任务：需要先登录 —— 登录后会自动执行。";
      return;
    }
    if (state.autoError) {
      node.classList.add("auto-status--warn");
      node.textContent = `自动任务：上次提交失败 —— ${state.autoError}`;
      return;
    }

    const chosen = collectTasks({ group: "auto" });
    if (chosen.length === 0) {
      node.classList.add("auto-status--warn");
      node.textContent = "自动任务：未启用任何任务。";
      return;
    }

    const modeSuffix = isDevMode() ? ` · 模式 ${runModeText()}` : "";

    if (state.running) {
      node.classList.add("auto-status--run");
      node.textContent = `自动任务：运行中…${modeSuffix}`;
      return;
    }

    const pending = chosen.filter((item) => !ranToday(item.name));
    if (pending.length > 0) {
      node.classList.add("auto-status--warn");
      node.textContent =
        `自动任务：本游戏日待执行 ${pending.length} 个（${pending.map((item) => titleOf(item.name)).join("、")}）${modeSuffix}`;
      return;
    }

    node.classList.add("auto-status--ok");
    node.textContent =
      `自动任务：本游戏日已完成 ${chosen.length} 个${state.nextReset ? ` · 下次重置 ${state.nextReset}` : ""}${modeSuffix}`;
  }

  /**
   * 自动执行编排：把"今天还没跑的自动任务"提交一次。
   *
   * :param trigger: 触发原因（"启动" / "勾选变动" / "补跑"），只用于日志与提示。
   *     ★ 2026-10-06：它现在**还决定要不要捎带 once_per_day 那批任务** ——
   *     只有 "启动" / "补跑"（= 开机那一下，以及它撞 409 后的补跑）才带上，
   *     "勾选变动"一律摘掉（见 ONCE_PER_DAY_TRIGGERS 的说明）。
   * :return: 无（失败会写进 state.autoError，由状态行显示）。
   *
   * 【为什么提交前要把活跃宝箱排到最后】
   * 宝箱按活跃点解锁，而活跃点靠前面的任务（礼包 / 各类任务）涨上去。
   * 排前面就会拿着"还没涨的活跃点"去问 —— 刚过 05:00 打开页面时最容易看到
   * "活跃度明明够了却领不到"。规格表里它已排在最后，这里再显式排一次，
   * 将来有人调整规格顺序也不会把这条保证弄丢。
   */
  async function runAutomation(trigger) {
    const session = state.session || {};
    if (!session.has_session) {
      renderAutoStatus();
      return;
    }

    let todo = collectTasks({ group: "auto" }).filter((item) => !ranToday(item.name));
    // ★ 2026-10-06（用户要求"这些任务只在第一次登录工具时执行"）：
    // once_per_day 那批只在开机那一下自动提交。勾选变动不捎带 ——
    // 想立刻跑就点卡片上的「执行」小按钮（手动路径不经这里，优先级最高）。
    if (!ONCE_PER_DAY_TRIGGERS.has(trigger)) {
      todo = todo.filter((item) => !isOncePerDay(item.name));
    }
    todo.sort((a, b) => Number(REPEATABLE_AUTO.has(a.name)) - Number(REPEATABLE_AUTO.has(b.name)));
    if (todo.length === 0) {
      renderAutoStatus();
      return;
    }
    // ★ 2026-10-04：运行中**照常提交**（服务端会排队），不再等跑完再补。
    // 以前这里是"记下 pendingAuto、以后再补"，代价是"勾了任务要等很久才动"；
    // 现在队列保证顺序，自动任务也就是排在队尾而已。
    const result = await submitRun(todo, { reason: `自动任务（${trigger}）`, origin: "auto" });
    if (result === "ok") {
      // 只有确定当天只跑一次的任务在这里立即打上标记；yimo_box 需等 3 份宝箱全满由 pollOnce 标记
      const immediateMark = todo.filter((item) => item.name !== "yimo_box").map((item) => item.name);
      if (immediateMark.length > 0) markAutoRun(immediateMark);
      state.autoError = "";
    }
    renderAutoStatus();
  }

  /**
   * 失控炼成阵宝箱每小时轮询调度：
   * 登录期间每隔 1 小时自动重试一次，直到 3 份宝箱全部领满后彻底停止。
   */
  function scheduleYimoHourlyCheck() {
    if (ranToday("yimo_box")) return;
    if (state.yimoHourlyTimer) clearTimeout(state.yimoHourlyTimer);
    // 1 小时 = 3600 秒
    state.yimoHourlyTimer = setTimeout(async () => {
      state.yimoHourlyTimer = null;
      if (ranToday("yimo_box")) return;
      if (state.running) {
        state.yimoHourlyTimer = setTimeout(scheduleYimoHourlyCheck, 5 * 60 * 1000);
        return;
      }
      console.info("[yimo_box] 触发失控炼成阵宝箱每小时轮询...");
      await submitRun([{ name: "yimo_box", params: {} }], {
        reason: "失控炼成阵全服宝箱（每小时轮询）",
        origin: "auto",
      });
    }, 3600 * 1000);
  }

  // ------------------------------------------------------------------
  // 运行与进度
  // ------------------------------------------------------------------
  /**
   * 同步"是否有运行在进行中"到界面。
   *
   * 【为什么不是每次都重建任务卡片】
   * 这个函数会被轮询反复调用（运行中 1.2 秒一次）。重建 DOM 会打断用户正在
   * 输入的参数、滚动位置也会跳，所以只在"状态真的从运行中变成空闲"时才重建
   * 一次（执行按钮上的模式文案需要跟着演练开关刷新）。
   */
  function setRunning(running) {
    const wasRunning = state.running;
    state.running = running;
    // ★ 2026-10-04：执行中**不再禁用**按钮（禁用 = 排不了队）。
    // 改为让按钮在「执行 / 停止执行」两个状态之间切换（见 syncExecButtons）。
    if (wasRunning && !running) {
      // 队列跑空：所有"停止执行"复原成"执行"
      state.stopTasks.clear();
      renderTaskGroups();
    } else {
      syncExecButtons();
      renderAutoStatus();
    }
  }

  function renderProgress(run) {
    const list = $("progress");
    list.textContent = "";
    for (const step of run.steps || []) {
      const statusClass =
        step.status === "ok"
          ? "step--ok"
          : step.status === "fail"
            ? "step--fail"
            : step.status === "running"
              ? "step--running"
              : // ★ 2026-10-04：被用户中断而没执行的步骤 —— 它不是失败
                step.status === "skipped"
                ? "step--skipped"
                : "step--pending";
      list.appendChild(
        el("li", { class: `step ${statusClass}` }, [
          el("span", { class: "step-mark" }),
          el("div", {}, [
            el("div", { class: "step-name", text: step.title || step.name }),
            step.message ? el("div", { class: "step-msg", text: step.message }) : null,
            step.elapsed ? el("div", { class: "step-msg", text: `${step.elapsed} 秒` }) : null,
          ]),
        ])
      );
    }
    if (run.error) {
      list.appendChild(
        el("li", { class: "step step--fail" }, [
          el("span", { class: "step-mark" }),
          el("div", { class: "step-msg", text: run.error }),
        ])
      );
    }
    // 模式标记用**服务端回报的** run.dry_run：界面开关只是"意图"，
    // 这里显示的是"这一次到底发没发包"（见 renderProgressMode）。
    renderProgressMode(run.dry_run);
    // ★ 2026-10-04：队列里有排队批次时说一句 —— 用户点过的每一批都该看得见。
    const queuedHint =
      run.status === "running" && run.queued_runs > 0
        ? `（后台还有 ${run.queued_runs} 批在排队）`
        : "";
    $("progress-summary").textContent =
      run.status === "running"
        ? `运行中…${queuedHint}`
        : run.summary || (run.diamond_spent ? `已花 ${run.diamond_spent} 钻` : "");
  }

  /**
   * 提交一次运行并开始轮询（从原来的 `startRun` 里抽出的公共部分）。
   *
   * :param tasks: `[{name, params}]`；**顺序即执行顺序**（服务端串行跑）。
   * :param reason: 触发原因（进控制台，方便事后回答"这次是谁触发的"）。
   * :param origin: `"auto"`（自动执行）或 `"manual"`（手动点执行）。
   *     决定失败时是否写 state.autoError —— 那是「自动任务」状态行的数据源，
   *     手动执行的失败已有弹窗告知，再记进去会错位显示成
   *     "自动任务：上次提交失败"，像自动执行坏了（2026-09-30 修）。
   * :return: 状态字串：`"ok"` 已受理（HTTP 202）／`"busy"` 运行闸门被占（409）／
   *     `"auth"` 令牌问题／`"error"` 其它失败（**已经弹过提示**）。
   *
   * 【为什么返回状态字串而不是布尔值（2026-09-24 修）】
   * "忙"与"失败"对调用方的意义完全不同：
   * - **自动执行**遇到忙很正常 —— 记下 `pendingAuto`，等这次跑完自动补一次；
   * - **手动点「执行这个任务」**遇到忙必须**明确告诉用户**，否则现象是
   *   "点了没反应"，用户只会以为按钮坏了。
   *
   *   这不是假设：实测踩到过 —— 进主界面会自动执行每日任务，跑完那一刻
   *   `pollOnce` 又会调 `/api/player` 刷新信息面板（5 个只读任务串行），
   *   而它**占着同一把运行闸门**，此时点执行就是 409，静默丢掉用户的点击。
   *
   * 【为什么不在这里做二次确认】
   * 自动任务零消耗（不需要确认）；主动任务由 runManualTask 先确认再调用这里。
   */
  async function submitRun(tasks, { reason = "", origin = "manual" } = {}) {
    if (tasks.length === 0) return "error";
    try {
      const result = await call("/api/run", {
        method: "POST",
        body: { tasks, ...runOptions() },
      });
      $("progress-card").classList.remove("hidden");
      setRunning(true);
      renderProgress({ status: "running", steps: [] });
      console.info("run started", result.run_id, reason);
      // ★ 2026-10-04：排在别人后面时给一句明确反馈 ——
      // "点了没反应"是最容易让人以为按钮坏了的一种沉默。
      if (result.status === "queued" && result.queued_behind > 0) {
        appendRun("⏳", `已排入队列：${reason}（前面还有 ${result.queued_behind} 批）`, "info");
      }
      await pollOnce();
      schedulePolling();
      return "ok";
    } catch (error) {
      if (error.status === 401) {
        // 令牌失效：清掉并回登录页（那边会要求重新填，成功后跳回本页）
        window.Ct.clearToken();
        location.href = "login.html" + location.search;
        return "auth";
      }
      if (error.status === 409) {
        // 闸门被占（别的运行 / 只读查询正在跑）：不当作错误刷屏，
        // 只记下"待补一次"，并把状态字串交给调用方决定怎么提示。
        state.pendingAuto = true;
        renderAutoStatus();
        return "busy";
      }
      const message = describeError(error);
      // ★ 失败归属（2026-09-30）：只有自动执行路径才把失败记进 autoError；
      // 手动路径的用户已经看到弹窗，状态行不该跟着变红。
      if (origin === "auto") state.autoError = message;
      uiAlert(`${reason ? `【${reason}】` : ""}${message}`);
      renderAutoStatus();
      return "error";
    }
  }

  /**
   * 执行一个主动任务（用户点开卡片后按「执行这个任务」）。
   *
   * 【★ 2026-10-04：不再弹"上一次还没跑完"】
   * 服务端改成队列之后，运行中再提交是**合法且常见**的动作：
   * 它会挂在后台排队，按提交顺序全部跑完。弹窗拦住它 = 用户想排却排不上。
   *
   * 【为什么点了就直接执行，不再弹二次确认（2026-10-02 需求）】
   * 主动任务本来就是"用户点开卡片 → 自己把参数填好 → 再点执行"三步，
   * 每一步都是显式动作；再叠一层确认弹窗只是重复问同一件事，
   * 反而让人养成"闭眼点确定"的习惯 —— 那才是真正危险的操作习惯。
   * 消耗类信息的**告知**职责改由卡片上的「⚠ 消耗资源」徽标与参数提示承担。
   *
   * 【为什么要顺带补跑活跃宝箱】
   * 主动任务（扫荡 / 竞技场 / 宴席…）是**唯一**能让活跃点上涨的操作，而宝箱按
   * 活跃点解锁 —— 做完一个主动任务往往就多出一个可领的宝箱。所以把它排在同一个
   * 提交里，由服务端串行执行（顺序有保证）。
   *
   * 【为什么不发第二次 /api/run】
   * 运行闸门同一时刻只允许一次运行（第二次会拿到 409），而且分两次提交会让进度条、
   * 日志、消费额度断成两段、难以对应。一次运行两步，看得最清楚。
   */
  async function runManualTask(task, button) {
    const entry = state.selection.get(task.name);
    if (!entry) return;

    const jobs = [{ name: task.name, params: { ...entry.params } }];
    if (task.name !== BOX_TASK) jobs.push({ name: BOX_TASK, params: {} });

    try {
      const result = await submitRun(jobs, { reason: `主动任务：${task.title}` });
      if (result === "ok") {
        // 这一批还没跑完 → 本卡片的按钮变成「停止执行」
        state.stopTasks.add(task.name);
        if (button) button.disabled = false;
        syncExecButtons();
      }
    } finally {
      // 失败时按钮保持可用（不再由"运行中"禁用），跑完由 setRunning 复原
      if (button) button.disabled = !task.enabled;
    }
  }

  /**
   * 单独主动执行一个自动任务（用户点击自动任务卡片右上角的「执行」小按钮触发）。
   *
   * 【为什么需要单独执行（2026-10-03 用户需求）】
   * 满足特殊情况下的手动执行需求（如网络闪断后单项补领、掉线重领、或无需触发全部自动任务的即时执行）。
   * 1. 仅提交该任务本身，不串联其它任务（daily_box 本身也是清单中的独立任务）；
   * 2. 无论是否勾选自动执行或今日是否已跑，均允许手动单次触发；
   * 3. 成功后更新本日执行记录与状态行。
   */
  async function runSingleAutoTask(task, button) {
    const entry = state.selection.get(task.name);
    if (!entry) return;

    const jobs = [{ name: task.name, params: { ...(entry.params || {}) } }];

    try {
      const result = await submitRun(jobs, { reason: `主动执行：${task.title}`, origin: "manual" });
      if (result === "ok") {
        if (task.name !== "yimo_box") {
          markAutoRun([task.name]);
        }
        state.autoError = "";
        // 这一批还没跑完 → 本卡片的按钮变成「停止执行」
        state.stopTasks.add(task.name);
        if (button) button.disabled = false;
        syncExecButtons();
        renderAutoStatus();
      }
    } finally {
      if (button) button.disabled = !task.enabled;
    }
  }

  // ------------------------------------------------------------------
  // 日志（增量轮询 + 运行结果流水 + 底部坞 + 开发者日志导出）
  // ------------------------------------------------------------------
  //: 日志最多保留的行数（★ 2026-09-30）。
  //: 服务端缓冲是 2000 行的环形队列，但前端过去**无限累积** —— 长会话里
  //: 会攒下上万个节点，手机上滚动明显掉帧。800 行足够回看一次运行，
  //: 且远小于服务端上限，被裁掉的也只是"早就在别处看不到了"的旧日志。
  const LOG_DOM_LIMIT = 800;

  /**
   * 结果流水（★ 2026-10-01 用户要求"精简日志，只看运行结果"）：
   * 运行视图 / 底部坞不再搬运任务的 print 流 —— 那是脚本运行逻辑，
   * 重复又长。改成两条来源：
   *   ① /api/status 的步骤结果：每个任务**一行** `✔/✘ 标题 · 耗时 · 摘要`
   *     （见 renderRunResults，靠 lastRunId/seenSteps 去重）；
   *   ② WARNING/ERROR 日志行：只取首行（完整多行说明留给「保存」导出）。
   * 任务的 print 细节仍全部进 state.logLines 缓冲，由 saveDevLog 导出。
   */
  function nowTs() {
    return new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  //: 容器 id ↔ 帧缓冲键。
  //: 【为什么按容器 id 索引、而不是共用一份缓冲】两个视图的内容口径不同：
  //: dock-log 只吃"运行结果"（appendRun / renderRunResults），run-log 还额外
  //: 吃 appendLogLine 里的 WARNING/ERROR。共用一份缓冲会把 warn/error 日志行
  //: 灌进底坞，改变它的语义 —— 所以各记各的。
  //:
  //: 【2026-10-05 修复：底坞标题被超限裁剪吃掉】
  //: 旧 appendLogView 直接 appendChild 到容器，再 while(children.length > LIMIT)
  //: removeChild(firstChild) —— 而 firstChild 是**整个容器的第一个孩子**。
  //: 底坞容器里它正是 .bottom-log-head（「运行结果」标题 + 清屏/收起），
  //: 于是行数一旦触到 LOG_DOM_LIMIT，最先消失的就是标题整块，dock 变成一个
  //: 没有头、只剩滚动的裸面板（双端都能复现）。改为「帧缓冲 + 整体替换」后：
  //:   1) 裁剪只可能发生在 .log-line 上，永不会碰 head / resizer；
  //:   2) 环形截断改在内存数组上做，与 DOM 结构彻底解耦。
  const RUN_VIEW_BUFFERS = { "run-log": "runLines", "dock-log": "dockLines" };

  /** 取某个视图的帧缓冲；未知容器返回 null（调用方自行降级）。 */
  function runBuffer(container) {
    const key = container && RUN_VIEW_BUFFERS[container.id];
    return key ? state[key] : null;
  }

  /**
   * 帧缓冲追加：只保留最近的 LOG_DOM_LIMIT 行（环形）。
   * 在**内存数组**上截断是安全的 —— 这里在意的只有"行"，不存在标题节点。
   */
  function pushRunBuffer(buffer, row) {
    if (!buffer) return;
    buffer.push(row);
    if (buffer.length > LOG_DOM_LIMIT) buffer.splice(0, buffer.length - LOG_DOM_LIMIT);
  }

  /**
   * 追加一行到运行结果视图（帧缓冲 + 整体替换，见 RUN_VIEW_BUFFERS 的说明）。
   * 之所以不直接 appendChild 到容器：容器的第一个孩子不是日志行而是面板
   * 结构（底坞的 .bottom-log-head），基于 children.length 的裁剪会误杀它。
   */
  function appendLogView(container, row) {
    if (!container) return;
    const buffer = runBuffer(container);
    if (!buffer) {
      // 兜底：不在已知视图表里的容器（未来新增），沿用最简单的追加
      container.appendChild(row);
      return;
    }
    pushRunBuffer(buffer, row);
    renderRunBuffer(container);
  }

  /**
   * 把帧缓冲整体落成 DOM。
   * 用 DocumentFragment 一次性替换：避免逐行 remove/add 造成的重排闪烁，
   * 也让"裁剪"只可能作用在 .log-line 上。
   */
  function renderRunBuffer(container) {
    const buffer = runBuffer(container);
    if (!buffer) return;
    const frame = document.createDocumentFragment();
    for (const row of buffer) frame.appendChild(row);
    container.textContent = "";
    container.appendChild(frame);
  }

  /** 清空某个运行结果视图（含帧缓冲）—— 清屏按钮与 init 共用。 */
  function clearRunView(container, placeholderText) {
    const buffer = runBuffer(container);
    if (buffer) buffer.length = 0;
    if (!container) return;
    container.textContent = "";
    if (placeholderText) {
      container.appendChild(el("p", { class: "log-placeholder", text: placeholderText }));
    }
  }

  //: 两个视图共用的占位文案（改一处即可，别再各写一份）
  const RUN_VIEW_PLACEHOLDER =
    "还没有运行结果 —— 任务执行后会在这里逐条列出 ✔ / ✘ 与一句话摘要";

  /** 清掉"还没有结果"的占位提示（每个容器只在第一行到来前显示）。 */
  function clearRunPlaceholder(container) {
    const placeholder = container && container.querySelector(".log-placeholder");
    if (placeholder) placeholder.remove();
  }

  /**
   * 往运行结果视图与底部坞各追加一行。
   * :param countUnread: 首次历史回填传 false —— 那是"以前发生的事"，
   *   不该点亮未读徽标。
   * :param at: 这一行的**真实发生时刻**（``HH:MM:SS``），由服务端给出
   *   （步骤的 ``finished_at`` / 日志行的 ``ts``）。
   *   ★ 2026-10-06 修复"瞬间同时执行"假象：以前这里一律用 ``nowTs()``
   *   （= 渲染这一刻），于是页面在运行**之后**才加载时，回填的十几行会
   *   顶着同一个时间戳 —— 一批严格串行、耗时合计约 30 秒的任务看起来像
   *   "瞬间同时跑完"。拿不到服务端时刻才退回本地时间。
   */
  function appendRun(mark, text, level, { countUnread = true, at = "" } = {}) {
    for (const id of ["run-log", "dock-log"]) {
      const container = $(id);
      if (!container) continue;
      clearRunPlaceholder(container);
      appendLogView(
        container,
        // 【2026-10-06】不再拼 log-line--${level} / log-mark--${markClass}：
        // 分级配色已弃用（对应规则也从 input.css 删了）。level 只作为
        // data-level 留在 DOM 上，便于排查/将来要用时不必回改调用点。
        el("span", { class: "log-line", "data-level": level || "" }, [
          mark ? el("span", { class: "log-mark", text: `${mark} ` }) : null,
          el("span", { class: "log-time", text: `${at || nowTs()} ` }),
          el("span", { class: "log-msg", text }),
        ])
      );
    }
    scrollLogToBottom();
    if (countUnread && dockEnabled() && !dockIsExpanded()) {
      state.unread += 1;
      updateDockBadge();
    }
  }

  function appendLogLine(line, { countUnread = true } = {}) {
    state.logLines.push(line);
    if (state.logLines.length > LOG_DOM_LIMIT) state.logLines.shift();
    if (line.level === "WARNING" || line.level === "ERROR") {
      appendRun(
        line.level === "ERROR" ? "✘" : "⚠",
        String(line.message).split("\n")[0],
        line.level,
        // ★ 2026-10-06：用**服务端**记这条日志的时刻，而不是我们读到它的时刻
        // （断线重连后会一次读回一批，全盖成"现在"就分不清先后了）。
        { countUnread, at: line.ts || "" }
      );
    }
  }

  /**
   * 把 /api/status 的步骤结果落成"结果流水"：新运行先画一条模式分隔线，
   * 然后每个任务完成时记一行（✔/✘ + 标题 + 耗时 + 后端给的一句话摘要）。
   * 采用唯一 step_id 单向去重：重复执行相同任务或多次自动宝箱均能如实展示，且清屏后不复活旧记录。
   *
   * 【时间戳一律用服务端给的（★ 2026-10-06）】
   * 分隔线用 ``status.started_at``（这一批**真正开始**的时刻），
   * 结果行用 ``step.finished_at``（这一步**真正结束**的时刻）。
   * 以前两处都写 ``nowTs()``，于是"页面在运行之后才打开/刷新"时，
   * ``/api/status`` 会一次性回填最近 10 批已完成步骤 —— 十几行顶着同一个
   * 时间戳蹦出来，看着就像"瞬间并发跑完"，而实际是严格串行的
   * （单 worker 线程 + 顺序 for 循环，见 ``services/runner.py``）。
   * 拿不到服务端字段时才退回本地时间。
   */
  function renderRunResults(status) {
    if (status.run_id && status.run_id !== state.lastRunId) {
      state.lastRunId = status.run_id;
      const mode = status.dry_run === false ? "真实执行" : "演练（不发包）";
      const startedAt = status.started_at || nowTs();
      for (const id of ["run-log", "dock-log"]) {
        const container = $(id);
        if (!container) continue;
        clearRunPlaceholder(container);
        appendLogView(
          container,
          el("span", { class: "log-line log-divider", text: `── ${mode} · ${startedAt} ──` })
        );
      }
      scrollLogToBottom();
    }
    for (const step of status.steps || []) {
      if (step.status !== "ok" && step.status !== "fail") continue;
      const key = step.step_id || `${step.run_id || status.run_id || "run"}:${step.name}:${step.status}`;
      if (state.renderedStepIds.has(key)) continue;
      state.renderedStepIds.add(key);
      const parts = [step.title || step.name];
      if (step.elapsed) parts.push(`${step.elapsed}s`);
      if (step.message) parts.push(String(step.message).split("\n")[0]);
      appendRun(
        step.status === "ok" ? "✔" : "✘",
        parts.join(" · "),
        step.status === "ok" ? "USER" : "ERROR",
        { at: step.finished_at || "" }
      );
    }
    if (status.error && status.run_id) {
      const errKey = `${status.run_id}:error`;
      if (!state.renderedStepIds.has(errKey)) {
        state.renderedStepIds.add(errKey);
        appendRun("✘", String(status.error).split("\n")[0], "ERROR");
      }
    }
  }

  function scrollLogToBottom(force = false) {
    // 两个视图各自独立滚动：只在"本来就贴着底部"时跟随，
    // 用户往上翻历史时不会被拽回去
    for (const id of ["run-log", "dock-log"]) {
      const container = $(id);
      if (!container) continue;
      const nearBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight < 80;
      if (force || nearBottom) container.scrollTop = container.scrollHeight;
    }
  }

  let lastSavedLogFile = "";

  /**
   * 复制纯文本到剪贴板，支持标准 Clipboard API 与 textarea 兼容回退。
   */
  async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        /* 继续尝试兼容兜底 */
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "-9999px";
    textarea.setAttribute("readonly", "");
    document.body.appendChild(textarea);
    textarea.select();
    let success = false;
    try {
      success = document.execCommand("copy");
    } catch {
      success = false;
    }
    document.body.removeChild(textarea);
    return success;
  }

  /**
   * 安卓壳注入的 JS 桥（MainActivity 的 LogExporter，注入名 `MiyaAndroid`）。
   * 桌面 EXE / 普通浏览器里不存在该对象 → 返回 null，调用方自动走 PC 分支。
   */
  function androidBridge() {
    const bridge = window.MiyaAndroid;
    return bridge && typeof bridge.exportLog === "function" ? bridge : null;
  }

  /**
   * 安卓端的「再导出一份」：让原生壳把服务端刚写好的日志文件复制到
   * 公共「下载/米娅小助手/」目录。
   *
   * 【为什么必须这么做（2026-10-06 用户报障）】安卓端 Python 的可写根目录是
   * 应用**内部**私有存储（`/data/user/0/<包名>/files`）—— 文件确实写成功了，
   * 但无 root 的文件管理器与 USB 都读不到，用户点完「保存」在手机里根本找不到。
   * 而 targetSdk 34 的分区存储又禁止 Python 直接 open() 公共目录（必 EACCES），
   * 所以只能由 Kotlin 侧走 MediaStore 写入「下载」。
   *
   * 跨桥只传「源路径 + 文件名」，日志正文不过桥（缓冲可能有几 MB）。
   *
   * @returns {{ok: boolean, path: string, note: string}}
   *   ok=true 时 path 为用户可见路径；失败或不支持时 path 回退为服务端原路径、note 为原因。
   */
  function exportLogOnAndroid(filepath, filename) {
    const bridge = androidBridge();
    if (!bridge || !filepath) {
      return { ok: false, path: filepath || "", note: "" };
    }
    try {
      const out = JSON.parse(bridge.exportLog(filepath, filename || "") || "{}");
      if (out && out.ok) {
        return { ok: true, path: out.path || filepath, note: "" };
      }
      return {
        ok: false,
        path: filepath,
        note: (out && out.message) || "导出失败（原因未知）",
      };
    } catch (e) {
      return {
        ok: false,
        path: filepath,
        note: `导出失败（${(e && e.message) || e}）`,
      };
    }
  }

  /**
   * 把缓冲里的全量日志持久化保存至本地磁盘 logs/ 目录（双端统一落盘），
   * 并在卡片下方展示简约状态标与存储路径，提供「打开目录」与「复制路径」功能。
   *
   * 安卓端在服务端落盘之后会**再导出一份**到公共「下载/米娅小助手/」，
   * 并把展示路径换成用户能真正找到的那一条（见 exportLogOnAndroid）。
   */
  async function saveDevLog(button) {
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "保存中…";

    const text = state.logLines
      .map(
        (line) =>
          `${line.ts || "--:--:--"} [${line.level}] ${line.logger}: ${line.message}`
      )
      .join("\n");

    try {
      const res = await call("/api/logs/save", {
        method: "POST",
        body: { content: text || "" },
      });

      const exported = exportLogOnAndroid(res.filepath, res.filename);
      lastSavedLogFile = exported.path || res.filepath || "";

      const badge = $("devlog-save-badge");
      const pathEl = $("devlog-saved-path");
      const labelEl = $("devlog-saved-label");
      const statusEl = $("devlog-saved-status-text");
      const openBtn = $("btn-devlog-open-folder");

      if (pathEl) pathEl.textContent = lastSavedLogFile;
      if (labelEl) labelEl.textContent = exported.ok ? "已导出到：" : "存储路径：";
      if (statusEl) {
        statusEl.textContent = exported.ok
          ? "日志已保存并导出到「下载」目录"
          : "日志已保存至本地文件";
      }
      if (openBtn) {
        openBtn.classList.toggle(
          "hidden",
          !(res.can_open_folder || androidBridge())
        );
      }
      if (badge) badge.classList.remove("hidden");

      button.textContent = "已保存";
      if (exported.ok) {
        showToast(`日志已导出到「${exported.path}」（共 ${res.line_count} 行）`);
      } else if (exported.note) {
        showToast(`日志已保存，但${exported.note}（共 ${res.line_count} 行）`);
      } else {
        showToast(`日志已保存至：${res.filename}（共 ${res.line_count} 行）`);
      }
      setTimeout(() => {
        button.textContent = originalText;
      }, 2000);
    } catch (e) {
      showToast(`保存日志失败：${describeError(e)}`);
      button.textContent = originalText;
    } finally {
      button.disabled = false;
    }
  }

  /** 打开日志存放目录（安卓走原生桥打开系统「下载」界面） */
  async function openDevLogFolder() {
    const bridge = androidBridge();
    if (bridge && typeof bridge.openDownloadsFolder === "function") {
      try {
        const out = JSON.parse(bridge.openDownloadsFolder() || "{}");
        if (out && out.ok) {
          showToast("已打开系统「下载」界面");
        } else {
          showToast(
            (out && out.message) || "无法打开下载目录，请在文件管理器中查看"
          );
        }
      } catch (e) {
        showToast(`打开目录失败：${(e && e.message) || e}`);
      }
      return;
    }
    try {
      const res = await call("/api/logs/open_folder", { method: "POST" });
      if (res && res.ok) {
        showToast("已在文件管理器中打开日志目录");
      } else {
        showToast((res && res.message) || "当前平台不支持直接打开目录，请使用复制路径");
      }
    } catch (e) {
      showToast(`打开目录失败：${describeError(e)}`);
    }
  }

  /** 复制日志文件路径 */
  async function copyDevLogPath(button) {
    const path =
      lastSavedLogFile ||
      ($("devlog-saved-path") && $("devlog-saved-path").textContent) ||
      "";
    if (!path) {
      showToast("暂无已保存的文件路径");
      return;
    }
    const ok = await copyToClipboard(path);
    if (ok) {
      const original = button.textContent;
      button.textContent = "已复制 ✔";
      showToast("日志路径已复制到剪贴板");
      setTimeout(() => {
        button.textContent = original;
      }, 1500);
    } else {
      showToast("复制失败，请长按路径文本手动选择复制");
    }
  }

  // ------------------------------------------------------------------
  // 底部日志坞（2026-10-01，交互参考 lucima-tools 的 bottom-log-dock）
  //
  // 收起 = 右下角悬浮 peek 按钮（新运行日志 → 未读徽标）；
  // 展开 = 固定底部的运行日志面板，顶缘 resizer 可拖拽调高。
  // 开关在「日志记录」分区（.log-dock-setting），偏好与高度都进 localStorage。
  // ------------------------------------------------------------------
  function dockEnabled() {
    return document.body.classList.contains("bottom-log-enabled");
  }

  function dockIsExpanded() {
    const dock = $("bottom-log-dock");
    return !!dock && dock.classList.contains("expanded");
  }

  function updateDockBadge() {
    const badge = $("dock-badge");
    if (!badge) return;
    badge.textContent = state.unread > 99 ? "99+" : String(state.unread);
    badge.classList.toggle("hidden", state.unread === 0);
  }

  /** 开/关坞（persist=false 用于启动恢复，不重复写盘）。 */
  function setBottomLogEnabled(on, persist = true) {
    document.body.classList.toggle("bottom-log-enabled", on);
    $("opt-bottom-log").checked = on;
    // 收起态才显示 peek 按钮；展开中面板本身就是入口
    $("bottom-log-peek").classList.toggle("hidden", !on || dockIsExpanded());
    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEYS.bottomLog, on ? "1" : "0");
      } catch {
        /* 隐私模式：记不住偏好，不影响本次 */
      }
    }
  }

  function setBottomLogExpanded(expanded) {
    const dock = $("bottom-log-dock");
    if (!dock) return;
    dock.classList.remove("hidden");
    dock.classList.toggle("expanded", expanded);
    $("bottom-log-peek").classList.toggle("hidden", !dockEnabled() || expanded);
    document.body.classList.toggle("bottom-log-expanded", expanded);
    if (expanded) {
      state.unread = 0;
      updateDockBadge();
      applyDockHeight();
      scrollLogToBottom(true);
      setTimeout(() => scrollLogToBottom(true), 320);
    }
  }

  /** 把高度写进 CSS 变量并钳位；persist=true 时记住。 */
  function applyDockHeight(persist = false) {
    const isMobile = window.innerWidth < 900;
    const minH = isMobile ? 160 : 120;
    const maxH = Math.max(minH + 40, Math.round(window.innerHeight * (isMobile ? 0.72 : 0.6)));
    state.dockHeight = Math.min(Math.max(state.dockHeight, minH), maxH);
    document.documentElement.style.setProperty(
      "--bottom-log-h",
      `${state.dockHeight}px`
    );
    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEYS.bottomLogH, String(state.dockHeight));
      } catch {
        /* 同上，只是记不住 */
      }
    }
  }

  function initBottomLogDock() {
    let enabled = true; // 默认开：坞是"新日志有提示"的来源，关掉是显式偏好
    try {
      if (localStorage.getItem(STORAGE_KEYS.bottomLog) === "0") enabled = false;
    } catch {
      /* 读不到就按默认 */
    }
    try {
      const saved = parseInt(localStorage.getItem(STORAGE_KEYS.bottomLogH) || "", 10);
      if (saved > 0) state.dockHeight = saved;
    } catch {
      /* 同上 */
    }
    if (!state.dockHeight || state.dockHeight < 160 || (window.innerWidth < 640 && state.dockHeight > 360)) {
      const isMobile = window.innerWidth < 900;
      state.dockHeight = isMobile
        ? Math.min(240, Math.max(180, Math.round(window.innerHeight * 0.35)))
        : Math.min(300, Math.round(window.innerHeight * 0.38));
    }
    applyDockHeight();
    setBottomLogEnabled(enabled, false);

    $("opt-bottom-log").addEventListener("change", (event) => {
      setBottomLogEnabled(event.target.checked);
    });
    $("bottom-log-peek").addEventListener("click", () => setBottomLogExpanded(true));
    $("btn-dock-collapse").addEventListener("click", () => setBottomLogExpanded(false));
    $("btn-dock-clear").addEventListener("click", () => {
      clearRunView($("dock-log"), RUN_VIEW_PLACEHOLDER);
    });

    // resizer 拖拽：优先 Pointer Events（消除移动端与 touch 重复监听冲突）
    const resizer = document.querySelector(".bottom-log-resizer");
    if (resizer) {
      let startY = 0;
      let startHeight = 0;
      let isDragging = false;

      const startDrag = (clientY) => {
        isDragging = true;
        startY = clientY;
        startHeight = state.dockHeight;
        document.body.classList.add("dock-resizing");
      };

      const moveDrag = (clientY) => {
        if (!isDragging) return;
        state.dockHeight = startHeight + (startY - clientY);
        applyDockHeight();
      };

      const endDrag = () => {
        if (!isDragging) return;
        isDragging = false;
        document.body.classList.remove("dock-resizing");
        applyDockHeight(true);
      };

      if (window.PointerEvent) {
        // Pointer Events（桌面与现代移动端 WebView）
        resizer.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          try {
            resizer.setPointerCapture(event.pointerId);
          } catch (_) {}
          startDrag(event.clientY);
          const onPointerMove = (e) => moveDrag(e.clientY);
          const onPointerUp = (e) => {
            resizer.removeEventListener("pointermove", onPointerMove);
            resizer.removeEventListener("pointerup", onPointerUp);
            resizer.removeEventListener("pointercancel", onPointerUp);
            try {
              resizer.releasePointerCapture(event.pointerId);
            } catch (_) {}
            endDrag();
          };
          resizer.addEventListener("pointermove", onPointerMove);
          resizer.addEventListener("pointerup", onPointerUp);
          resizer.addEventListener("pointercancel", onPointerUp);
        });
      } else {
        // 原生 Touch fallback
        resizer.addEventListener("touchstart", (e) => {
          if (e.touches && e.touches.length === 1) {
            startDrag(e.touches[0].clientY);
          }
        }, { passive: false });
        resizer.addEventListener("touchmove", (e) => {
          if (isDragging && e.touches && e.touches.length === 1) {
            e.preventDefault();
            moveDrag(e.touches[0].clientY);
          }
        }, { passive: false });
        resizer.addEventListener("touchend", () => endDrag());
        resizer.addEventListener("touchcancel", () => endDrag());
      }
    }
    // 窗口变尺寸后重新钳位（最大化 / 缩放后高度不越界）
    window.addEventListener("resize", () => applyDockHeight());
  }

  async function fetchLogs() {
    // 首次拉取 = 把服务端缓冲里的**历史**搬进来：那是"以前发生的事"，
    // 不该点亮未读徽标（countUnread=false 一路传下去）
    const backfill = state.cursor === 0;
    const payload = await call("/api/logs", { params: { since: state.cursor } });
    if (payload.truncated) {
      appendLogLine(
        {
          ts: "",
          level: "WARNING",
          logger: "webapi",
          message: "（日志行数超过缓冲上限，较早的行已被丢弃）",
        },
        { countUnread: !backfill }
      );
    }
    for (const line of payload.lines) appendLogLine(line, { countUnread: !backfill });
    state.cursor = payload.next_seq;
    scrollLogToBottom();
  }

  // ------------------------------------------------------------------
  // 轮询：进度 + 日志
  // ------------------------------------------------------------------
  async function pollOnce() {
    // ★ 并发守卫（2026-09-30）：submitRun 起跑后会"立刻"调一次 pollOnce，
    // 而定时轮询可能正好在飞。两次并发 fetchLogs 会用同一个 state.cursor
    // 各取一遍增量 → 同一批日志行被追加两遍。已有一份在跑就直接跳过：
    // schedulePolling 马上会带着最新 cursor 再来一次，不会丢状态。
    if (state.polling) return;
    state.polling = true;
    try {
      const status = await call("/api/status");
      const wasRunning = state.running;
      // 服务端记账的"今天跑过哪些"：运行期间/刚跑完都跟着刷新
      if (Array.isArray(status.auto_done)) state.autoDone = new Set(status.auto_done);
      // ★ 宴席"哪顿吃过了"也随轮询刷新（与 auto_done 同理）：吃完那一刻按钮就该灰。
      if (status.banquet) state.banquet = status.banquet;
      setRunning(status.status === "running");
      if (state.running || status.run_id) $("progress-card").classList.remove("hidden");
      if (status.run_id) {
        renderProgress(status);
        // 结果流水：每个任务完成时在「运行结果」/底部坞记一行 ✔/✘
        renderRunResults(status);
        // 若步骤中包含 push_main_stage，即时用其最新数据刷新状态
        const mainStep = (status.steps || []).find((s) => s.name === "push_main_stage");
        if (mainStep && mainStep.data && mainStep.data.highest_passed_desc) {
          if (!state.mainStageStatus || state.mainStageStatus.highest_passed_id !== mainStep.data.highest_passed_id) {
            state.mainStageStatus = {
              highest_passed_id: mainStep.data.highest_passed_id,
              highest_passed_desc: mainStep.data.highest_passed_desc,
              next_section_id: mainStep.data.next_section_id,
              next_section_desc: mainStep.data.next_section_desc,
            };
            renderTaskGroups();
            renderInfo();
          }
        }
        // ★ 宴席（2026-10-05）：跑完的那一步自带两顿的最新窗口视图（任务结果里的
        // ``meals`` 字段），就地刷新按钮四态 —— 不用再发一次 32007 去问。
        const bbqStep = (status.steps || []).find((s) => s.name === "bbq_energy");
        if (bbqStep && bbqStep.data && Array.isArray(bbqStep.data.meals)) {
          state.banquetMeals = bbqStep.data.meals;
        }
        // 若步骤中包含 yimo_box，根据是否 3 份宝箱已全部领满决定是否完成或排下一次轮询
        const yimoStep = (status.steps || []).find((s) => s.name === "yimo_box");
        if (yimoStep && yimoStep.status === "ok" && yimoStep.data) {
          if (yimoStep.data.all_claimed === true) {
            markAutoRun(["yimo_box"]);
            if (state.yimoHourlyTimer) {
              clearTimeout(state.yimoHourlyTimer);
              state.yimoHourlyTimer = null;
            }
          } else {
            scheduleYimoHourlyCheck();
          }
        }
      }

      await fetchLogs();
      // 运行中 → busy（绿点呼吸），空闲 → ok（静态绿点）：只靠颜色 + 动效区分，
      // 不再靠文字（见上方 setConn 的注释）。
      setConn(state.running ? "busy" : "ok", state.running ? "任务运行中" : "已连接");

      if (wasRunning && !state.running) {
        // 刚跑完：刷新状态行与资源数字。用户跑完任务最关心的就是
        // "体力/钻石变了多少"，自动刷一次比让他自己去找「刷新」更自然。
        // ★ 荣耀之巅例外：只有这次运行**真的操作过它**才顺带读一次。
        const touchedTopPvp = (status.steps || []).some(
          (step) => step.name === "top_pvp_battle"
        );
        renderAutoStatus();
        loadPlayer({ full: touchedTopPvp });
        refreshMainStageStatus();
      }
      // ★ 运行期间被挡下的自动执行在这里补一次（见 runAutomation 的 pendingAuto）。
      // 【2026-10-04 修】旧条件（wasRunning 迁移）覆盖不了"提交 409 时本页还没见过
      // 任何运行"的场景 —— 典型就是刚登录：/api/player 只读还在跑、自动提交拿到
      // 409，而之后轮询永远见不到迁移，补跑就永远不会发生。改为"空闲且有欠账就补"。
      if (state.pendingAuto && !state.running) {
        state.pendingAuto = false;
        await runAutomation("补跑");
      }
    } catch (error) {
      if (error.status === 401) {
        // 轮询时发现令牌失效：停止本页轮询，回登录页重新建立身份。
        setConn("bad", "需要令牌");
        window.Ct.clearToken();
        location.href = "login.html" + location.search;
        return;
      }
      setConn("bad", "服务不可用");
    } finally {
      // 无论成败都释放并发守卫：失败不留锁，下一次轮询照常能进来。
      state.polling = false;
    }
  }

  /**
   * 自己排下一次轮询（而不是用 setInterval）。
   *
   * 【为什么要动态间隔】运行中要"看起来实时"（1.2 秒一次）；空闲时那么勤会让
   * 手机一直亮着 WiFi 白耗电。空闲 5 秒一次既够用又安静。
   */
  function schedulePolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    const delay = state.running ? 1200 : 5000;
    state.pollTimer = setTimeout(async () => {
      await pollOnce();
      schedulePolling();
    }, delay);
  }

  async function loadTasks() {
    const payload = await call("/api/tasks");
    state.groups = payload.groups || [];
    state.tasks = payload.tasks || [];
    state.views = new Map(state.tasks.map((task) => [task.name, task]));
    // ★ 游戏日（2026-09-24）：后端按"游戏服务器每天 05:00 换日"算好的两个字段。
    // 自动执行的"今天跑过没有"完全靠它 —— 前端不自己算日期（见 state.gameDay 的说明）。
    state.gameDay = payload.game_day || "";
    state.nextReset = payload.next_daily_reset || "";
    // ★ 服务端记账的"今天已自动跑过"（2026-10-04）：权威判重来源，见 ranToday。
    state.autoDone = new Set(payload.auto_done || []);
    // ★ 宴席"哪顿吃过了"（2026-10-05）：服务端记账，与 auto_done 无关。
    if (payload.banquet) state.banquet = payload.banquet;
    // ★ 任务勾选/参数（2026-10-05）：**以服务端保存值为权威**。
    // saved_updated_at 为空 = 服务端从未存过（升级后首启）—— 保留 localStorage
    // 里既有的选择；有值则把服务端那份灌进 localStorage 再走既有 reconcile
    // （类型归一、默认值继承、消失任务丢弃全部复用原逻辑）。
    const savedSelection = payload.selection || {};
    const hasServerSelection = Boolean(savedSelection.saved_updated_at);
    if (hasServerSelection && savedSelection.saved_tasks &&
        typeof savedSelection.saved_tasks === "object") {
      try {
        localStorage.setItem(
          STORAGE_KEYS.selection,
          JSON.stringify(savedSelection.saved_tasks)
        );
      } catch {
        /* 存储被禁用时忽略：reconcile 会退回默认勾选 */
      }
    }
    // 演练模式：默认隐藏且为真实执行；只有在开发模式（?dev=1）下才显式出现
    const devMode = isDevMode();
    const runOptionsCard = $("run-options-card");
    if (runOptionsCard) {
      runOptionsCard.classList.toggle("hidden", !devMode);
    }
    // 演练开关的还原同样是"服务端权威 + localStorage 兜底"：
    // saved_dry_run 为 null（服务端从未记录）时才读 localStorage。
    const storedDryRun =
      typeof savedSelection.saved_dry_run === "boolean"
        ? savedSelection.saved_dry_run
        : readStoredDryRun();
    if (hasServerSelection) saveStoredDryRun(storedDryRun);
    const dryRunCheckbox = $("opt-dry-run");
    if (dryRunCheckbox) {
      dryRunCheckbox.checked = devMode ? (storedDryRun === true) : false;
    }
    // ★ 钻石使用总闸（2026-10-05）：**以服务端保存值为权威**还原勾选框。
    // localStorage 在 EXE 的 Edge --app 壳里不可靠（profile 在 tmp/ 下，
    // 清理即丢），它只作服务端字段缺失时的兜底。还原完成后置
    // allowDiamondSynced = true：此后提交的 allow_diamond 才被后端视为
    // 用户此刻的明确选择（否则后端按「已保存值 或 请求值」合并）。
    // 这样"开了就一直是开的、关了就一直关着"，跨重启 / 跨壳都不再重置。
    const allowDiamondCheckbox = $("opt-allow-diamond");
    if (allowDiamondCheckbox) {
      const savedSpend = payload.spend || {};
      const saved =
        typeof savedSpend.saved_allow_diamond === "boolean"
          ? savedSpend.saved_allow_diamond
          : readStoredAllowDiamond();
      allowDiamondCheckbox.checked = saved;
      state.allowDiamondSynced = true;
    }
    reconcileSelection();
    // ★ 活动区域 / 关卡候选：**过期才联网**（每周三 20:00 之后，或首次使用）。
    // 命中缓存时这里是零请求 —— 用户明确要求"不要每次都自动拉取，一周才变一次"。
    // 未登录时它会立刻返回，卡片上的状态行会如实写"需要先登录"。
    await ensureActivityCache();
    // ★ 素材关卡与元素试炼静态子目录树：固定配置，零网络发包
    await ensureMaterialTree();
    // renderTaskGroups 内部会顺带刷新自动任务状态行（原来这里是 syncRunModeLabel）
    renderTaskGroups();
  }

  // ------------------------------------------------------------------
  // 事件绑定与启动
  // ------------------------------------------------------------------
  function wireEvents() {
    $("btn-logout").addEventListener("click", doLogout);

    // --- 侧边栏导航（宽屏纵向 / 窄屏横向，同一批 .tab 按钮） ---
    $("tabs").addEventListener("click", (event) => {
      const tab = event.target.closest(".tab");
      if (tab) setTab(tab.dataset.tab);
    });
    // 窄屏导航两端渐隐随滚动位置更新（详见 syncTabsScrollHint）。
    // 也挂 load：OPPO Sans 是打包字体，字体就位前后 tab 宽度会变，
    // 滚动范围跟着变 —— 只算一次会把状态算错。
    $("tabs").addEventListener("scroll", syncTabsScrollHint, { passive: true });
    window.addEventListener("resize", syncTabsScrollHint);
    window.addEventListener("load", syncTabsScrollHint);
    $("btn-refresh-info").addEventListener("click", () => loadPlayer());
    const dryRunEl = $("opt-dry-run");
    if (dryRunEl) {
      dryRunEl.addEventListener("change", () => {
        saveStoredDryRun(dryRunEl.checked);
        // ★ 2026-10-05：演练开关随 selection 一并双写服务端（防抖合并）。
        saveSelection();
        renderTaskGroups();
      });
    }
    // ★ 钻石使用总闸（2026-10-05）：变化即双写 —— localStorage 兜底 + 服务端权威。
    // 服务端保存失败必须**可见**（这是花钱的闸）：否则界面开着、重启又回关，
    // 用户会以为开关坏了。失败不回滚勾选框 —— 本次提交按用户所见生效，
    // 下次启动会回到服务端最后保存成功的位置。
    const allowDiamondEl = $("opt-allow-diamond");
    if (allowDiamondEl) {
      allowDiamondEl.addEventListener("change", async () => {
        saveStoredAllowDiamond(allowDiamondEl.checked);
        try {
          await call("/api/spend", {
            method: "POST",
            body: { allow_diamond: Boolean(allowDiamondEl.checked) },
          });
        } catch (error) {
          uiAlert(
            "⚠ 钻石总闸没能保存到服务端：" + describeError(error) +
              "\n本次运行仍按界面所示生效，但重启后可能回到上次保存的位置。"
          );
        }
      });
    }

    // --- 日志（「日志记录」分区） ---
    // 开发者日志默认不展开：唯一动作是导出文件（级别/暂停/清空/复制已随
    // 展开视图一起移除 —— 那些都是"看流"的工具，现在只留"存档"）
    $("btn-devlog-save").addEventListener("click", (event) => {
      saveDevLog(event.target);
    });
    const openFolderBtn = $("btn-devlog-open-folder");
    if (openFolderBtn) {
      openFolderBtn.addEventListener("click", () => {
        openDevLogFolder();
      });
    }
    const copyPathBtn = $("btn-devlog-copy-path");
    if (copyPathBtn) {
      copyPathBtn.addEventListener("click", (event) => {
        copyDevLogPath(event.currentTarget || event.target);
      });
    }
    // 运行日志卡的小工具
    $("btn-runlog-clear").addEventListener("click", () => {
      clearRunView($("run-log"), RUN_VIEW_PLACEHOLDER);
    });
    $("btn-runlog-copy").addEventListener("click", () => copyLog("run-log", "btn-runlog-copy"));
  }

  async function copyLog(viewId, buttonId) {
    const text = Array.from($(viewId).children)
      .filter((row) => !row.hidden)
      .map((row) => row.textContent)
      .join("\n");
    const button = $(buttonId);
    try {
      // 手机通过局域网 IP 访问时不是"安全上下文"，clipboard 可能不可用
      await navigator.clipboard.writeText(text);
      button.textContent = "已复制";
    } catch {
      window.prompt("手动复制下面的日志：", text);
    }
    setTimeout(() => {
      button.textContent = "复制";
    }, 1500);
  }

  // ------------------------------------------------------------------
  // 外观设置（2026-10-03）：iOS 双模外观切换 + 自动跟随系统
  // ------------------------------------------------------------------
  function initThemeSettings() {
    const cardLight = $("theme-card-light");
    const cardDark = $("theme-card-dark");
    const autoSwitch = $("opt-theme-auto");
    if (!cardLight || !cardDark || !autoSwitch) return;

    function getSystemTheme() {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";
    }

    function applyTheme(theme, isAuto, smooth = true) {
      if (smooth) {
        document.documentElement.classList.add("theme-transitioning");
        setTimeout(() => document.documentElement.classList.remove("theme-transitioning"), 280);
      }
      document.documentElement.setAttribute("data-theme", theme);
      cardLight.classList.toggle("theme-card--active", theme === "light");
      cardDark.classList.toggle("theme-card--active", theme === "dark");
      autoSwitch.checked = isAuto;
      localStorage.setItem(STORAGE_KEYS.theme, theme);
      localStorage.setItem(STORAGE_KEYS.themeAuto, isAuto ? "true" : "false");
    }

    // 初始化状态读取
    const savedAuto = localStorage.getItem(STORAGE_KEYS.themeAuto);
    const isAuto = savedAuto === "true" || savedAuto === null;
    const savedTheme = localStorage.getItem(STORAGE_KEYS.theme);
    const currentTheme = isAuto ? getSystemTheme() : (savedTheme || "dark");

    applyTheme(currentTheme, isAuto, false);

    cardLight.addEventListener("click", () => {
      applyTheme("light", false, true);
    });

    cardDark.addEventListener("click", () => {
      applyTheme("dark", false, true);
    });

    autoSwitch.addEventListener("change", () => {
      if (autoSwitch.checked) {
        applyTheme(getSystemTheme(), true, true);
      } else {
        const activeTheme = document.documentElement.getAttribute("data-theme") || "dark";
        applyTheme(activeTheme, false, false);
      }
    });

    // 监听系统深浅色切换事件
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
        if (localStorage.getItem(STORAGE_KEYS.themeAuto) === "true") {
          applyTheme(e.matches ? "light" : "dark", true, true);
        }
      });
    }
  }

  /**
   * 窗口控制（最小化 / 最大化 / 关闭）交互与过渡动画。
   * 支持桌面 pywebview 桥调用，并在点击时触发平滑过渡动画：
   * - 最小化：整个文档平滑缩放、下沉并淡出（win-minimizing），随后收进系统任务栏；窗口恢复焦点时平滑缩放还原（win-restoring）。
   * - 最大化：切换最大化与还原状态，触发 win-maximizing-transition 布局过渡。
   * - 关闭：平滑淡出（win-closing）后销毁窗口。
   */
  function initWindowControls() {
    const minBtn = document.getElementById("win-btn-min");
    const maxBtn = document.getElementById("win-btn-max");
    const closeBtn = document.getElementById("win-btn-close");

    if (minBtn) {
      minBtn.addEventListener("click", () => {
        minBtn.classList.add("win-btn--active-anim");
        document.documentElement.classList.add("win-minimizing");
        setTimeout(() => {
          minBtn.classList.remove("win-btn--active-anim");
          const api = window.pywebview && window.pywebview.api;
          if (api && typeof api.minimize === "function") {
            api.minimize();
          }
        }, 180);
      });
    }

    if (maxBtn) {
      maxBtn.addEventListener("click", () => {
        maxBtn.classList.add("win-btn--active-anim");
        setTimeout(() => maxBtn.classList.remove("win-btn--active-anim"), 260);

        const isMax = maxBtn.classList.toggle("is-maximized");
        const maxSvg = `<svg class="win-icon-max" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="2.5" width="11" height="11" rx="2"/></svg>`;
        const restoreSvg = `<svg class="win-icon-restore" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="1.5" width="10" height="10" rx="1.5"/><path d="M1.5 4.5v10h10"/></svg>`;
        maxBtn.innerHTML = isMax ? restoreSvg : maxSvg;
        maxBtn.title = isMax ? "还原" : "最大化";
        maxBtn.setAttribute("aria-label", maxBtn.title);

        document.documentElement.classList.add("win-maximizing-transition");
        setTimeout(() => {
          document.documentElement.classList.remove("win-maximizing-transition");
        }, 260);

        const api = window.pywebview && window.pywebview.api;
        if (api && typeof api.toggle_maximize === "function") {
          api.toggle_maximize();
        }
      });
    }

    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        closeBtn.classList.add("win-btn--active-anim");
        document.documentElement.classList.add("win-closing");
        setTimeout(() => {
          const api = window.pywebview && window.pywebview.api;
          if (api && typeof api.close === "function") {
            api.close();
          }
        }, 150);
      });
    }

    // 窗口恢复焦点时（从最小化恢复），触发回弹放大过渡动画
    window.addEventListener("focus", () => {
      if (document.documentElement.classList.contains("win-minimizing")) {
        document.documentElement.classList.remove("win-minimizing");
        document.documentElement.classList.add("win-restoring");
        setTimeout(() => {
          document.documentElement.classList.remove("win-restoring");
        }, 250);
      }
    });
  }

  function start() {
    wireEvents();
    initThemeSettings();
    initWindowControls();

    // 令牌的 URL/会话存储交接逻辑在 common.js（两页共用同一份）
    window.Ct.initToken();
    // 底部日志坞：恢复开关/高度偏好并接上交互（在 boot 前完成，
    // 这样"进主界面自动执行"产生的日志从一开始就有地方提示）
    initBottomLogDock();

    // 记住上次看的分区（默认停在「信息展示」）
    setTab(sessionStorage.getItem(STORAGE_KEYS.tab) || "info");
    renderInfo(); // 立即渲染面板骨架，保证用户进页面瞬间可见！
    boot();
  }

  document.addEventListener("DOMContentLoaded", start);
})();
