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
    // ★ 2026-10-18：启动时"信息面板让行"的欠账标记。
    //
    // 【为什么需要它】服务端改成**写优先**（见 ``webapi/state.py::run_inline``
    // 的"让行"说明）：队列里有批在跑/排队时，``/api/player`` 会直接 409。
    // 启动时若已经交上了自动批，就不能再发 loadPlayer（发一次白挨一个 409，
    // 面板还会闪"读取失败"）。把"该读一次完整数据"记在这里，等这批跑完由
    // ``pollOnce`` 的"刚跑完"分支用 ``full=1`` 补读（含荣耀之巅 —— 等价于
    // 原来 boot 里那次"登录进来的统一读取"）。
    bootFullPlayerPending: false,
    version: "",
    // ★ 自动任务（2026-09-24）：/api/tasks 带回的游戏日与"下次重置时刻"。
    // "今天跑过没有"完全依赖 gameDay —— 它就是"游戏服务器每天 05:00 换日"
    // 这条规则的**唯一**口径（后端算好，前端只比较字符串）。
    gameDay: "",
    nextReset: "",
    // ★ 2026-10-16 重做：哪些任务"正在执行 / 还在队列里"。
    //
    // 【真源在服务端】每次轮询从 ``/api/status`` 的 ``steps`` 里挑出 status 为
    // pending/running 的步骤 —— 服务端把"正在跑的那一批 + 排队批次的 pending 步骤"
    // 拼成一条列表给前端（见 ``webapi/state.py`` 的 ``status()``）。前端**不再自己
    // 记账**：自己记的账刷新页面就丢（这正是旧 ``stopTasks`` 的老毛病），而且
    // 看不见"不是本页面提交的那一批"。
    //
    // 三个字段分工：
    //   ``activeTasks``   —— 轮询派生的任务名集合（唯一准绳）
    //   ``activeRunIds``  —— 任务名 → 它所属批次的 run_id（供「精确停止」用）
    //   ``optimisticRuns``—— 提交成功到下一次轮询之间的**过渡值**，只为"点下去
    //                        立刻有反馈"；轮询一到就清空（见 pollOnce）
    activeTasks: new Set(),
    activeRunIds: new Map(),
    optimisticRuns: new Set(),
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
    // ★ 2026-10-16：那份"两顿状态"的**失效时刻**（毫秒，服务端给的
    // ``expires_at``）。超过它就必须重发 32007/32008 —— 否则会拿过期窗口
    // 回答"现在能不能吃"（用户要的"登录时算一次"是"拉一次然后缓存"，
    // 因为窗口起止只存在于服务端响应里，本地推不出来）。
    banquetExpiresAt: 0,
    // ★ 2026-10-18：宴席时段的读取状态（用户反馈"读取失败刷屏"的落点）。
    // ``banquetLoading``：有请求在飞（并发锁 —— 开机自动拉 / 展开卡片 /
    //   点「刷新」可能同时发生，各发一次只会各撞一次 409）；
    // ``banquetPending``：因为运行中没读到，欠一次"运行结束后自动补读"；
    // ``banquetRetryAt``：下一次补读的最早时刻（节流，避免自动重试把
    //   32007/32008 这种风控可见的流量刷出来）；
    // ``banquetRetryDelay`` / ``banquetRetryCount``：指数退避（5→15→45→60s）
    //   与次数上限 —— 没有上限时，只要闸门被只读快照长期占着就会无限重试。
    banquetLoading: false,
    banquetPending: false,
    // ★ 2026-10-19：欠读的原因（"busy" = 运行占着闸门 / "timeout" = 读超时）。
    // 只影响宴席卡头部那行文案 —— 两种情况都靠同一套退避补读，
    // 但"任务运行中"对超时是在撒谎，必须分开说。
    banquetPendingReason: "",
    banquetRetryAt: 0,
    banquetRetryDelay: 5000,
    banquetRetryCount: 0,
    // ★ 2026-10-19：任务清单的"补水"阶段（见 loadTasks / refreshHydratedCards）。
    // 首帧只等 /api/tasks，候选（活动/素材）与宴席时段随后补 ——
    // 这段窗口里参数下拉与宴席卡都要如实说"正在读取…"，而不是
    // "尚无候选 / 尚未读取（点刷新）"，否则用户会以为读失败了。
    hydrating: false,
    // ★ 2026-10-18：本轮"信息面板快照 + 主线状态"预计占用闸门的截止时刻。
    // 它们都是 run_inline，与宴席抢同一把锁；快照在飞时补读必然 409，
    // 所以在那之前 retryBanquetAfterRun 直接跳过（快照链结束会清零）。
    snapshotBusyUntil: 0,
    // ★ 2026-10-16：一键执行的**成员顺序**（服务端 ``task_spec.batch_tasks()``
    // 排好的名单）。顺序规则只在服务端实现一次，前端照着用 ——
    // 自己再按 batch_order 排一遍就等于同一套规则写两处，迟早不一致。
    batchTasks: [],
    // ★ 2026-10-16：一键执行提交成功后的批次号。它让「停止执行」能
    // **精确撤掉自己这一批**（可能还在排队，而服务端的 /api/stop 默认
    // 停的是"正在跑的那一批" —— 那就停错对象了）。
    batchRunId: null,
    // 最近一次**提交成功**的批次号（submitRun 写，调用方读）。
    // ⚠️ 与 ``lastRunId`` 不是一回事：那个是"结果流水已经渲染到哪一批"
    // （见 renderRunResults），用途完全不同，别合并。
    submittedRunId: null,
    // ★ 2026-10-16：已展开的主动任务名。**服务端权威 + 本地兜底**（见
    // services/ui_prefs.py，随 /api/tasks 的 ui_prefs 下发）。
    // 以前它只存内存：刷新页面一律折叠，理由是"免得上次点开的那张卡看起来像
    // 它还在执行/还在排队"。那条理由现在不成立了 —— "在不在执行"由按钮自己的
    // 「停止执行」红描边态如实表达（而且那个态是从服务端派生的）。
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
  //: 【为什么要有这个数】★ 2026-10-18 起接口只下发 **SSR**（SR / R 已在
  //: ``/api/player`` 里被挡掉），但 SSR 仍可能有几十条（真机实测 86 个）。
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

      // 拉取任务清单（自动任务唯一的依赖），然后**先把自动任务交上去**。
      const tasksReady = loadTasks();
      await tasksReady;
      // ★「每次启动脚本就自动执行」：进入主界面即触发一次自动任务
      // （今天已经跑过的会被 ranToday 挡掉，所以反复刷新页面不会重复发包）
      await runAutomation("启动");

      // ★ 2026-10-18（让行）：信息面板**排在自动任务之后**。
      //
      // 【旧写法为什么不行】过去这里先 ``void loadPlayer({full:true})`` 再提交
      // 自动任务。后端当时是"先到先得抢闸门"，而 /api/player 要串行跑 6 个
      // 只读任务（实测 10~30 秒）—— 它几乎必然先抢到，自动任务只能排在后面等，
      // 表现就是"已受理运行，但第一个任务迟迟不开始"。
      // 2026-10-04 只去掉了这里的 ``await``，让提交早一点发生，但闸门仍被读占着，
      // 延迟并没有真正消失。
      //
      // 【现在的契约】服务端写优先（RunBusyError 让行）：有批在跑/排队时，
      // 只读查询直接 409。所以这里分两种情况：
      //   · 自动批已受理 → 不读，记一笔欠账，等它跑完由 pollOnce 用 full=1 补读；
      //   · 没有自动批（当天已跑过 / 没勾选）→ 立刻读，此时没人跟它抢。
      if (state.running) {
        state.bootFullPlayerPending = true;
        // 让面板把"任务运行中，结束后自动读取"显示出来（见 renderInfo）。
        renderInfo();
      } else {
        // ★ 登录进来的这次是"统一读取"：连荣耀之巅一起读一次
        // （之后就以这份快照为准，见 loadPlayer 的说明）。
        // 主线状态**排在它之后**：两者抢同一把闸门，并行发就是白挨一个 409。
        void loadPlayer({ full: true, quiet: true }).finally(() => refreshMainStageStatus());
      }

      // 日志常驻：任务与后端动态都在这里看
      await pollOnce();
      schedulePolling();

      // ★ 2026-10-18 兜底：自动批可能在上面的 pollOnce 之前就跑完了
      // （例如勾选项全被"今天已跑过"挡掉，提交后瞬间收尾）——
      // 那样 pollOnce 的 wasRunning→idle 迁移永远见不到，欠账会一直挂着。
      // 这里就地结清：只要已经不忙、且欠着一次完整读取，就补一次。
      if (state.bootFullPlayerPending && !state.running) {
        state.bootFullPlayerPending = false;
        void loadPlayer({ full: true, quiet: true })
          .then((ok) => {
            // 还是被让行挡下（别的窗口/别的批在跑）→ 留着欠账，下次再补。
            if (!ok) state.bootFullPlayerPending = true;
          })
          .finally(() => refreshMainStageStatus());
      }
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
   * 界面图标的唯一字典：**与顶栏 / 侧栏导航同一套线性语言** ——
   * 24×24 视框、`stroke-width 1.8`、圆头圆角、`stroke: currentColor`。
   *
   * 【为什么统一换成内联 SVG（2026-10-18 用户拍板全量统一）】
   * 原来的任务图标和指标图标是原游戏素材：厚描边 + 渐变 + 高光。问题有三：
   * ① 与导航那排线性图标是两套语言，同屏看着像两个产品；
   * ② 26px 的方框里塞几十条细节，缩小就糊成一团（船舵、旗帜最明显）；
   * ③ 必须垫一层圆角灰底 + 投影才能压住彩色，深色卡片上发脏。
   * 线性图标缩小不糊、颜色随文字自动适配深浅主题、零额外请求。
   * 原 PNG 全部保留在 ``web/assets/icons/`` 下，回滚只需还原本文件与 input.css。
   *
   * 键名是**语义**而不是文件名 —— 同一个图标被多处复用（如 swords 既给
   * 「天梯竞技场」任务、也给「竞技次数」指标卡），改一处两边同时生效。
   * 值是一段 SVG 内胆（只含子元素，外层的 svg 属性由 ``iconSvg`` 统一加）。
   */
  const ICON_PATHS = {
    // —— 任务卡片 ——
    mail: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.8 7.3 12 12.8l8.2-5.5"/>',
    gift: '<path d="M3.5 8.5h17V11h-17z"/><path d="M5 11v7.4A2.6 2.6 0 0 0 7.6 21h8.8a2.6 2.6 0 0 0 2.6-2.6V11"/><path d="M12 8.5V21"/><path d="M12 8.5C11 5.6 9.4 4.4 8.1 5.1 6.9 5.8 7.6 8.5 12 8.5z"/><path d="M12 8.5c1-2.9 2.6-4.1 3.9-3.4 1.2.7.5 3.4-3.9 3.4z"/>',
    // ★ 2026-10-18：好友赠礼「领取」专用 —— 礼盒 + 向下的箭头（"收进来"）。
    // 不复用 gift：它与「特惠礼包免费领取」在同一组并排，撞图会分不清。
    gift_in: '<path d="M3.5 8.5h17V11h-17z"/><path d="M5 11v7.4A2.6 2.6 0 0 0 7.6 21h8.8a2.6 2.6 0 0 0 2.6-2.6V11"/><path d="M12 8.5v7.2"/><path d="M9.9 13.6 12 15.7l2.1-2.1"/><path d="M12 8.5C11 5.6 9.4 4.4 8.1 5.1 6.9 5.8 7.6 8.5 12 8.5z"/><path d="M12 8.5c1-2.9 2.6-4.1 3.9-3.4 1.2.7.5 3.4-3.9 3.4z"/>',
    calendar_check: '<rect x="3" y="5" width="18" height="15" rx="2.5"/><path d="M8 3v4M16 3v4M3 10h18"/><path d="M9 14.4l2 2 4-4"/>',
    pickaxe: '<path d="M4 20.5 14.2 8.8"/><path d="M8.8 7.6c3.2-1.6 6.6-.8 8.8 1.8"/><path d="M17.4 6.2 19.6 8.6 17 9.9z"/>',
    users: '<circle cx="8.6" cy="8" r="3"/><circle cx="16.8" cy="9.6" r="2.3"/><path d="M3 20.5c0-3.1 2.5-5.6 5.6-5.6s5.6 2.5 5.6 5.6"/><path d="M14.8 20.5c0-2.2 1.6-4 3.7-4s3.5 1.6 3.5 3.7"/>',
    coin_give: '<circle cx="12" cy="9" r="5.5"/><path d="M12 6.2v5.6M9.2 9h5.6"/><path d="M4 16.6c2.3 2 5 3 8 3s5.7-1 8-3"/>',
    compass: '<circle cx="12" cy="12" r="9"/><path d="M15.6 8.4 13.2 13.2 8.4 15.6l2.4-4.8z"/>',
    chest: '<path d="M3 10h18v8a2.6 2.6 0 0 1-2.6 2.6H5.6A2.6 2.6 0 0 1 3 18z"/><path d="M3 10l1.8-4.3a1.6 1.6 0 0 1 1.5-1h11.4a1.6 1.6 0 0 1 1.5 1L21 10"/><path d="M9.9 13.4h4.2v3.2H9.9z"/>',
    map: '<path d="M9 3.5 3.5 5.5v15l5.5-2 6 2 5.5-2v-15l-5.5 2z"/><path d="M9 3.5v15M15 5.5v15"/>',
    flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.4-.5-2-1-3-1.1-2.1-.2-4 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.2.4-2.3 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    bag: '<path d="M5.6 8h12.8l1.2 11.4a1.6 1.6 0 0 1-1.6 1.6H6a1.6 1.6 0 0 1-1.6-1.6z"/><path d="M9 8V6.2a3 3 0 0 1 6 0V8"/>',
    flag: '<path d="M6 21V3.5"/><path d="M6 4h11.4l-2.2 4.3 2.2 4.2H6"/>',
    swords: '<path d="M4.5 3.5 19.8 18.8M19.5 3.5 4.2 18.8"/><path d="M3.2 20.8l3.4-3.4M20.8 20.8l-3.4-3.4"/><path d="M3.2 3.4 6.8 4.3 4.2 7z"/><path d="M20.8 3.4 17.2 4.3 19.8 7z"/>',
    hexstar: '<path d="M12 3.2 19.6 7.6v8.8L12 20.8 4.4 16.4V7.6z"/><path d="M12 8l3 4-3 4-3-4z"/>',
    flask: '<path d="M9.2 3h5.6"/><path d="M10.3 3v6.6L4.9 18.1A2 2 0 0 0 6.6 21.1h10.8a2 2 0 0 0 1.7-3L13.7 9.6V3"/><path d="M7.6 15.2h8.8"/>',
    skull: '<path d="M12 3a7.6 7.6 0 0 0-7.6 7.6c0 2.7 1.3 4.5 3.1 5.6v2.6A2.2 2.2 0 0 0 9.7 21h4.6a2.2 2.2 0 0 0 2.2-2.2v-2.6c1.8-1.1 3.1-2.9 3.1-5.6A7.6 7.6 0 0 0 12 3z"/><circle cx="9.4" cy="10.4" r="1.4"/><circle cx="14.6" cy="10.4" r="1.4"/><path d="M11 14.6v1.1M13 14.6v1.1"/>',
    cutlery: '<path d="M6.6 3v6.4a2.2 2.2 0 0 0 4.4 0V3"/><path d="M8.8 3v18"/><path d="M17.6 3c-1.8 0-3 2.2-3 4.8V12h3v9"/>',
    bolt_plus: '<path d="M12.4 3 5 13.2h5.1L9 21l8-10.8h-5.2z"/><path d="M18.4 3.4v4M16.4 5.4h4"/>',
    stage: '<path d="M3 20.5h5v-5h5v-5h5"/><path d="M18 10.5V4"/><path d="M18 4h3.6l-1.1 2.2 1.1 2.2H18"/>',
    check_circle: '<circle cx="12" cy="12" r="9"/><path d="M8 12.4l2.6 2.6L16 9.6"/>',
    card_star: '<rect x="4.5" y="3.5" width="13" height="17" rx="2.5"/><path d="M8 3.5h6"/><path d="M11 8l1.3 2.7 2.9.4-2.1 2 .5 2.9L11 14.6 8.4 16l.5-2.9-2.1-2 2.9-.4z"/>',
    // —— 只读 / 状态类任务（2026-10-18 一并补齐：同列卡片不再有图标缺席） ——
    switch_account: '<path d="M4 8h13"/><polyline points="14 5 17 8 14 11"/><path d="M20 16H7"/><polyline points="10 13 7 16 10 19"/>',
    wallet: '<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10.2h18"/><path d="M16.2 14.6h1.8"/>',
    roster: '<circle cx="8.6" cy="8.6" r="3.2"/><path d="M3 20c0-3.1 2.5-5.6 5.6-5.6S14.2 16.9 14.2 20"/><path d="M15.6 6.6h5.2M15.6 10.4h5.2M15.6 14.2h3.4"/>',
    plug: '<path d="M8 9V3.5M16 9V3.5"/><path d="M5.5 9h13v3.6A6.5 6.5 0 0 1 12 19a6.5 6.5 0 0 1-6.5-6.4z"/><path d="M12 19v2"/>',
    checklist: '<path d="M4 6.6 5.7 8.3 8.4 5.4"/><path d="M11.4 6.9h9"/><path d="M4 12.6 5.7 14.3 8.4 11.4"/><path d="M11.4 12.9h9"/><path d="M4 18.6 5.7 20.3 8.4 17.4"/><path d="M11.4 18.9h9"/>',
    // —— 指标卡 / 玩家名片专属 ——
    shield: '<path d="M12 3.2 5 6v5.8c0 4.4 2.9 7.4 7 8.9 4.1-1.5 7-4.5 7-8.9V6z"/>',
    backpack: '<path d="M5.5 9.5h13v8.5a2.5 2.5 0 0 1-2.5 2.5H8a2.5 2.5 0 0 1-2.5-2.5z"/><path d="M9 9.5V7.5a3 3 0 0 1 6 0v2"/><path d="M5.5 13.5h13"/>',
    coin: '<circle cx="12" cy="12" r="8.5"/><path d="M14.6 9.4a3.7 3.7 0 1 0 0 5.2"/>',
    gem: '<path d="M7 3.4h10l4 6.1L12 20.8 3 9.5z"/><path d="M3 9.5h18"/>',
    user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20.5c0-3.7 3.2-6.6 7.2-6.6s7.2 2.9 7.2 6.6"/>',
    trophy: '<path d="M8 3.5h8v6a4 4 0 0 1-8 0z"/><path d="M8 5.5H5.2v1.3A3 3 0 0 0 8.2 9.8"/><path d="M16 5.5h2.8v1.3a3 3 0 0 1-3 3"/><path d="M12 13.5V17"/><path d="M8.5 20.5h7l-.8-3h-5.4z"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><ellipse cx="12" cy="12" rx="4" ry="9"/>',
    bolt: '<path d="M13 2.8 5.6 13.6H11L9.6 21.2 17.2 10.4H11.6z"/>',
  };

  /**
   * 造一个图标元素；``key`` 不在 ICON_PATHS 里时返回 null（调用方当"没图标"处理）。
   *
   * :param size: 显式写在 svg 上的是**兜底**尺寸 —— 正式尺寸由 class 决定
   *   （任务卡 20px / 玩家名片 14px）。CSS 还没加载完时不至于是 0×0 的空白。
   */
  function iconSvg(key, className, size = 20) {
    const body = ICON_PATHS[key];
    if (!body) return null;
    // SVG 必须落在 SVG 命名空间：``document.createElement("svg")`` 在 HTML
    // 文档里拿到的是未知元素，画不出来。交给 innerHTML 让解析器走 SVG 分支；
    // 内容是上方常量，不接受任何外部输入，不存在注入面。
    const host = document.createElement("span");
    host.innerHTML = `<svg class="${className}" viewBox="0 0 24 24" width="${size}" height="${size}" `
      + `fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" `
      + `stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
    return host.firstElementChild;
  }

  /**
   * 一张统计卡（2026-09-30 增加图标与配色档位）。
   *
   * :param icon: ICON_PATHS 的键（线性 SVG）或 emoji 文本（装饰用，标记
   *   aria-hidden，读屏不念）。旧写法 ``assets/…png`` 仍能渲染，纯属兜底 ——
   *   2026-10-18 起所有调用点都已改成键名。
   * :param tone: 值的配色档位（"gold" | "diamond" | "stamina" | "power"），
   *   由 styles.css 的 `.stat--<tone>` 决定颜色；缺省用默认前景色。
   *   【为什么放前端而不是后端】这是"数字重要程度的表达方式"，不是数据
   *   本身 —— 后端 /api/player 只描述事实，怎么呈现归界面管。
   */
  function statCard(label, value, hint, { icon = "", tone = "", extraClass = "" } = {}) {
    let iconEl = null;
    if (icon) {
      if (ICON_PATHS[icon]) {
        iconEl = iconSvg(icon, "stat-icon-svg");
      } else if (icon.includes("/") || icon.endsWith(".png")) {
        // 兜底：漏改的旧调用点（PNG 路径）仍能显示，不至于整张卡丢图标。
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
              iconSvg("globe", "hero-tag-ico", 14),
              el("span", { text: session.server_name ? `#${session.server_id} ${session.server_name}` : (hasData ? "未连接区服" : "区服读取中…") }),
            ]),
            el("span", { class: "hero-tag" }, [
              iconSvg("shield", "hero-tag-ico", 14),
              el("span", { text: arena.alliance_name || (hasData ? "无公会" : "公会读取中…") }),
            ]),
          ]),
        ]),
      ]),
      el("div", { class: "hero-power-box" }, [
        el("span", { class: "hero-power-label" }, [
          iconSvg("trophy", "stat-icon-svg"),
          el("span", { text: "综合战力" }),
        ]),
        el("span", { class: "hero-power-val", text: formatNumber(arena.my_fighting) }),
      ]),
    ]);
    grid.appendChild(heroCard);

    // ② 分组一：核心资产（金币 / 钻石 / 体力）
    const assetTitle = el("div", { class: "stat-group-title", text: "核心资产" });
    const assetGrid = el("div", { class: "stat-grid stat-grid--assets" }, [
      statCard("游戏金币", formatNumber(game.coins), "", { icon: "coin", tone: "gold", extraClass: "stat--asset" }),
      statCard("钻石", formatNumber(game.diamonds), "", { icon: "gem", tone: "diamond", extraClass: "stat--asset" }),
      statCard("体力", formatNumber(game.stamina), "", { icon: "bolt", tone: "stamina", extraClass: "stat--asset" }),
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
      statCard("竞技次数", formatNumber(arena.challenge_count), arena.buyed_count ? `已购 ${formatNumber(arena.buyed_count)} 次` : "", { icon: "swords" }),
      statCard("天命对决次数", formatNumber(wudou.challenge_times), (hasData && wudou.my_ranking != null) ? `名次 ${formatNumber(wudou.my_ranking)}｜NPC ${formatNumber(wudou.npc_times)}` : "", { icon: "hexstar" }),
      statCard("失控炼成阵次数", formatNumber(yimo.remain_count), (hasData && yimo.now_score != null) ? `积分 ${formatNumber(yimo.now_score)}｜排名 ${formatNumber(yimo.now_rank)}` : "", { icon: "skull" }),
      statCard("荣耀之巅", rankText, subText, { icon: "trophy", tone: "power" }),
    ]);
    grid.appendChild(dailyTitle);
    grid.appendChild(dailyGrid);

    // ④ 分组三：进度与背包
    const mainStage = player.main_stage || state.mainStageStatus || {};
    const progTitle = el("div", { class: "stat-group-title", text: "进度与探索" });
    const progGrid = el("div", { class: "stat-grid stat-grid--progress" }, [
      statCard("最高主线通关", mainStage.highest_passed_desc || "—", mainStage.next_section_desc ? `下一关：${mainStage.next_section_desc}` : "", { icon: "stage", tone: "stamina" }),
      statCard("背包条目", formatNumber(game.item_count), hasData ? "道具总种类数" : "", { icon: "backpack" }),
    ]);
    grid.appendChild(progTitle);
    grid.appendChild(progGrid);

    // ⑤ 分组四：我的角色（★ 2026-10-05 新增）
    //
    // 数据来自只读任务 roles（5011 实时名册；拿不到时自动退回登录 1004 的快照）。
    // ★ 必须显示"这份数据是什么时候/从哪来的"：实时名册与登录快照的**新鲜度
    //   完全不同**，不说清楚用户会以为数字是实时的，从而怀疑数据不对
    //   （与荣耀之巅那张卡片同一套做法）。
    // ★ 2026-10-18：接口下发的 ``items`` **已经只含 SSR**（SR / R 在
    //   ``/api/player`` 里就被挡掉了，见 ``webapi/app.py::player_info``）。
    //   前端不做任何稀有度判断 —— 那要读配置表，属于后端口径。
    //   只有一个例外要在这里说：``rarity_filter === "unavailable"``
    //   表示属性表没读到、后端 fail-open 全量透出，必须明示"这次没过滤"。
    const roleItems = Array.isArray(roles.items) ? roles.items : [];
    const roleSourceText = roles.source === "live"
      ? "实时名册"
      : (roles.source === "snapshot" ? "登录快照" : "");
    const roleFilterWarn = roles.rarity_filter === "unavailable"
      ? "角色属性表没读到，本次未按品质过滤"
      : "";
    const roleHint = [roleSourceText ? `来源：${roleSourceText}` : "", roleFilterWarn]
      .filter(Boolean)
      .join("｜");
    // ★ 2026-10-06：列表可能很长（接口过滤后实测仍有 86 个 SSR）。
    //   全铺开会把这一栏撑得比上面三组加起来还长，把「进度与探索」挤出首屏。
    //   所以默认只铺一屏（前 ROLE_PREVIEW_LIMIT 个），其余收在标题右侧的
    //   「展开全部」后面；条数不超过一屏时没有折叠的必要，按钮不出现。
    const roleTotal = roleItems.length;
    const roleCollapsible = roleTotal > ROLE_PREVIEW_LIMIT;
    const rolesExpanded = roleCollapsible ? state.rolesExpanded : true;
    const roleShown = rolesExpanded ? roleItems : roleItems.slice(0, ROLE_PREVIEW_LIMIT);
    // 空态（★ 2026-10-18）：有角色但一个 SSR 都没有（例如新号只有 R）。
    // 只剩一张「SSR 角色 0」卡片会让人以为面板坏了 —— 明说一句"是全空
    // 还是被过滤空了"。``total_count`` 缺失（旧接口）时不猜，宁可不显示。
    const roleTotalCount = Number(roles.total_count || 0);
    const roleEmpty = hasData && roleTotal === 0 && roleTotalCount > 0
      ? el("div", {
          class: "role-empty",
          text: `没有 SSR 角色（全名册 ${formatNumber(roleTotalCount)} 个都不是 SSR）`,
        })
      : null;

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
      // 卡片口径（2026-10-18 用户拍板）：**只显示 SSR 数**，不摆全量、
      // 也不摆"已排除几个" —— 数字就是指列表里这些角色。
      statCard(
        "SSR 角色",
        hasData ? formatNumber(roles.count) : "—",
        roleHint || (hasData ? "暂无角色数据" : "读取中…"),
        { icon: "user" },
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
      roleEmpty,
    ]);
    grid.appendChild(roleTitle);
    grid.appendChild(roleGrid);

    const updateEl = $("info-updated");
    if (updateEl) {
      updateEl.textContent = hasData
        ? `上次刷新：${new Date().toLocaleTimeString("zh-CN")}`
        : // ★ 2026-10-18：启动时信息面板给自动任务让行，整批跑完才读 ——
          // 空面板要自己说明原因，否则看起来像"信息展示坏了"。
          state.bootFullPlayerPending
          ? "任务运行中，结束后自动读取"
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
   * :param quiet: 409（任务正在跑，服务端只读让行）是否**静默**。
   *   自动路径（启动补读 / 运行结束补读）传 ``True`` —— 那是预期内的让行，
   *   不是故障，弹提示只会让"每次跑完都闪一下"；用户手点「刷新」时传 ``False``，
   *   给一句 toast，免得"点了没反应"。
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
  async function loadPlayer({ full = false, quiet = false } = {}) {
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
      return true;
    } catch (error) {
      if (error.status === 409) {
        // ★ 2026-10-18：让行 —— 服务端有批在跑/排队时，只读查询会被拒绝。
        // 这不是故障：那批跑完 pollOnce 会自动用 full=1 补读一次。
        // 自动路径保持静默（否则每次跑完都闪一下提示），手点刷新才说一句。
        if (!quiet) showToast("任务正在执行，信息面板会在运行结束后自动刷新");
        console.info("信息面板让行：任务正在执行，稍后自动补读", describeError(error));
        return false;
      }
      const box = $("info-errors");
      if (box) {
        box.textContent = `读取失败：${describeError(error)}`;
        box.classList.remove("hidden");
      }
      return false;
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
  // 展开状态：本地兜底 + 服务端权威（★ 2026-10-16）
  //
  // 【为什么也要搬到服务端】理由与 selection / 钻石总闸**完全相同**：
  // EXE 窗口走 Edge --app，profile 在 tmp/ 下，一清理存储就丢；换端口 / 换壳
  // 之间 localStorage 也互不相通。而"上次点开的卡片还开着"是用户看得见的
  // 界面记忆，丢了会让人觉得工具"记不住东西"。
  //
  // 【为什么要防抖】展开/折叠是连点动作（用户会把好几张卡依次点开），
  // 500ms 内的连续变更合并成一次 POST（提交的永远是 state.expanded 全量）。
  //
  // 【失败为什么只 console.warn 而不弹窗】它只是界面记忆，丢了最坏结果是
  // "刷新后收起"，没有安全方向；只有钻石总闸那种"花钱的闸"才必须弹窗可见。
  // ------------------------------------------------------------------
  function readStoredExpanded() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.uiPrefs);
      const list = raw ? JSON.parse(raw) : [];
      return new Set(
        Array.isArray(list) ? list.filter((name) => typeof name === "string") : []
      );
    } catch {
      // 存坏了就当没有：界面偏好丢了无所谓，绝不能因此让页面打不开
      return new Set();
    }
  }

  let expandedSyncTimer = null;
  function saveExpandedPrefs() {
    try {
      localStorage.setItem(STORAGE_KEYS.uiPrefs, JSON.stringify([...state.expanded]));
    } catch {
      /* 隐私模式 / 存储被禁用时忽略 */
    }
    if (expandedSyncTimer) clearTimeout(expandedSyncTimer);
    expandedSyncTimer = setTimeout(async () => {
      expandedSyncTimer = null;
      try {
        await call("/api/ui_prefs", {
          method: "POST",
          body: { expanded: [...state.expanded] },
        });
      } catch (error) {
        console.warn("展开状态没能保存到服务端：", describeError(error));
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
  /**
   * 只刷新"由任务/参数派生出来的那几块"，**不重建任务卡片**。
   *
   * 【★ 2026-10-16：为什么需要它】
   * 以前改一个下拉、勾一个框都会调 renderTaskGroups() 整组重建 —— 代价不只是慢：
   * 别的卡片上「停止执行」的按钮会被重建成常态「执行」（就是这次的闪烁 bug），
   * 滚动位置也会跳。真正需要跟着变的其实只有钻石视图与三处状态行。
   * 现在的口径是"哪里真的变了就只改哪里"：单张卡片用 refreshCard()，
   * 全局派生视图用这个。
   */
  function refreshDerivedViews() {
    renderDiamondList();
    renderAutoStatus();
    // ★ 2026-10-16：一键执行的状态行（"已勾选几个 / 会跑哪些"）也要跟着重建
    renderManualBatchStatus();
  }

  /**
   * 重建**单张卡片**的参数区并重新施加它的展开态（不动别的卡片）。
   *
   * 【什么时候必须重建参数区】
   * 参数区的选项是"快照"式的：下拉里的候选来自 ``state.activityOptions`` /
   * ``state.materialTree``。这些数据是异步到的（更新区域 / 加载素材目录），
   * 到位之后不重造 DOM 的话，下拉里还是旧选项 —— 所以这一类变化必须刷新，
   * 但**只需要刷新那一张卡片**。
   *
   * :return: 是否找到了该卡片；找不到（例如它当前被过滤掉了）返回 ``False``，
   *     调用方可以退回整组重建。
   */
  function refreshCard(taskName) {
    const card = cardElementFor(taskName);
    const task = state.views.get(taskName);
    const entry = state.selection.get(taskName);
    if (!card || !task || !entry) return false;
    const isExpandable = task.group === "manual" || task.group === "diamond";
    const bodyOpen = isExpandable ? state.expanded.has(taskName) : Boolean(entry.checked);
    const old = card.querySelector(".task-params");
    if (old) old.replaceWith(paramsBoxFor(task, entry, bodyOpen));
    else card.append(paramsBoxFor(task, entry, bodyOpen));
    // 参数区换了新节点后把展开态重新施加一遍（顺带按最新状态重造执行控件）
    applyCardBodyOpen(card, task, bodyOpen);
    return true;
  }

  /**
   * 补水完成后，只重画"依赖异步候选数据"的卡片（★ 2026-10-19）。
   *
   * 【它解决什么】
   * `loadTasks()` 现在先调 `renderTaskGroups()` 出首帧（只等 `/api/tasks`），
   * 再依次补活动候选 / 素材目录 / 宴席时段。补水只影响两类东西：
   *   ① 参数里带 ``options_source`` 的下拉（活动区域/关卡、素材区域/关卡）；
   *   ② 宴席卡的两顿状态 —— 它自带 ``syncBbqPanel()`` 就地更新，这里只需补一次
   *      文案（``state.hydrating`` 从"正在读取…"切回真实状态）。
   * 其余卡片首帧时就已经是最终形态；重画它们只会换来闪烁与滚动跳动
   * （见 notes/exec_button_flicker_plan.md 的教训）。
   *
   * 【为什么找不到卡片时要整组重建】
   * `refreshCard()` 在卡片被过滤/容器未就绪时返回 false。补水阶段理论上不会
   * 发生，但真发生了（例如将来加了分组过滤）就得兜住 —— 宁可多画一次，
   * 也不能让下拉永远停在"（正在读取…）"。
   */
  function refreshHydratedCards() {
    let missed = false;
    for (const task of state.tasks) {
      if (task.hidden) continue;
      const needsOptions = (task.params || []).some((param) => param.options_source);
      if (!needsOptions) continue;
      if (!refreshCard(task.name)) missed = true;
    }
    if (missed) {
      renderTaskGroups();
      return;
    }
    // 宴席卡不在上面的名单里（参数没有 options_source）——单独补一次文案。
    syncBbqPanel(cardElementFor("bbq_energy"));
    refreshDerivedViews();
  }

  function renderTaskGroups() {
    renderTaskGroup("tasks-auto", "auto");
    renderTaskGroup("tasks-manual", "manual");
    renderTaskGroup("tasks-diamond", "diamond");
    refreshDerivedViews();
    // ★ 2026-10-16：整组重建之后补一次状态重放。
    // **正常情况用不着** —— 上面的 renderTask 在造按钮时就已经施加过状态
    // （见 applyExecButtonState，那才是"展开不闪"的真正修复点）。留着它是
    // 给"将来有人往渲染里加一条新路径"兜底，成本只是遍历一遍按钮。
    // ⚠️ 别把它当成依赖：谁要是把渲染路径的状态施加删掉，只靠这一句会退化成
    // "重建后补一次"的老写法 —— 那正是本次要根除的模式。
    syncExecButtons();
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

  // ------------------------------------------------------------------
  // 主动任务「一键执行」（★ 2026-10-16 用户需求）
  //
  // 【它到底是什么】
  // 把勾上的主动任务**按顺序拼成一次** ``/api/run`` 提交，每个任务后面
  // 照抄单卡执行的拼法各跟一个 ``daily_box``（宴席除外 —— 它的专用按钮
  // 刻意不串宝箱，语义要单一）。之所以必须"一次提交"而不是"逐个点执行"：
  // 服务端的 ``/api/stop`` 只中断**当前那一批**，队列里其他批次照跑；
  // 分成 N 批的话，用户点「停止执行」只能停掉手上那一件，
  // 后面排着的 5 批照跑 —— 而他要的是"从当前任务开始，后面全都别跑"。
  // ------------------------------------------------------------------

  /**
   * 本次一键执行**真正会提交**的任务名（按服务端给的顺序）。
   *
   * 【为什么"勾选"与"会跑"要分开算】
   * 勾选是"意愿"，会跑是"此刻的结论"：工会捐献今天已经捐过、宴席这一顿
   * 已经吃过，都不该再提交。状态行与按钮上的数字都必须用**后者**，
   * 否则会出现"显示将执行 3 个、实际只跑 2 个"这种让人以为漏跑了的错觉。
   *
   * 【"今天已捐过"为什么不写死任务名】
   * 判据用的是 ``view.once_per_day && state.autoDone.has(name)`` ——
   * 服务端已经把这套"每日一次"的账本下发了（``auto_done``），
   * 前端照读即可。写死 ``alliance_donate`` 就等于把名单抄了一份到 JS 里，
   * 将来再有一个"每天一次"的手动任务就会被漏掉。
   */
  function batchSelectionSummary() {
    //: 勾选且可批量的（含"本游戏日已经做过"的）。
    const checked = [];
    //: 其中此刻**真的会跑**的。
    const runnable = [];
    //: 勾了但本游戏日已记账（目前只有工会捐献）→ 状态行要说清楚。
    const done = [];
    for (const name of state.batchTasks) {
      const view = state.views.get(name);
      if (!view || view.hidden || !view.enabled || !view.batch_eligible) continue;
      const entry = state.selection.get(name);
      if (!entry || !entry.checked) continue;
      checked.push(name);
      if (view.once_per_day && state.autoDone.has(name)) done.push(name);
      else runnable.push(name);
    }
    return { checked, runnable, done };
  }

  /**
   * 宴席在一键执行里要提交哪几顿（``["morning"]`` / ``["evening"]`` / 两者 / ``[]``）。
   *
   * 【判据与卡片按钮完全一致（这是刻意的）】
   * 复用 ``bbqMealPanel`` 里那套"能不能点"的结论：没吃过、且后端说
   * ``edible``、且状态是 ``ready``（窗口内）或 ``makeup``（早宴席补吃）。
   * 用户明确要求"补吃也纳入一键"，所以 ``makeup`` 要提交。
   * 一旦两套判据分开写，就会出现"卡片上的按钮是灰的，一键却把它跑了"
   * 或者反过来的情况 —— 用户没法解释，只会觉得工具有 bug。
   *
   * 【为什么不吃 ``unknown``】
   * ``unknown`` = 还没联网读到时段。卡片上的按钮这时是可点的（"后端仍会把关"），
   * 但一键是**批量**动作，宁可不做也不要在信息不足时赌一把：
   * 早宴席在窗口外是会真的吃掉的（补吃），赌错有副作用。
   */
  function batchBanquetMeals() {
    const meals = [];
    const eaten = new Set((state.banquet && state.banquet.meals) || []);
    const live = Array.isArray(state.banquetMeals) ? state.banquetMeals : null;
    for (const meal of ["morning", "evening"]) {
      if (eaten.has(meal)) continue;
      const info = live ? live.find((item) => item.meal === meal) : null;
      if (!info) continue; // 没数据 → 不赌（见上面说明）
      if (info.edible === false) continue;
      if (info.state !== "ready" && info.state !== "makeup") continue;
      meals.push(meal);
    }
    return meals;
  }

  /** 把"待执行的勾选项"整理成提交用的 jobs（批次末尾跟一次宝箱）。 */
  function batchJobs(picked) {
    const jobs = [];
    for (const name of picked) {
      const entry = state.selection.get(name);
      if (!entry) continue;
      if (name === "bbq_energy") {
        // 宴席一天两顿，每顿一项（早在前、晚在后）
        for (const meal of batchBanquetMeals()) {
          jobs.push({ name, params: { ...entry.params, meal } });
        }
        continue;
      }
      jobs.push({ name, params: { ...entry.params } });
    }
    // 批次末尾统一领取一次日常宝箱，避免各任务之间重复多次查询领取
    if (jobs.length > 0) {
      jobs.push({ name: BOX_TASK, params: {} });
    }
    return jobs;
  }

  /** 全选 / 全不选：只作用于"参与一键执行且可用"的卡片（见 Q7 的决定）。 */
  function setAllBatchPicks(on) {
    for (const name of state.batchTasks) {
      const view = state.views.get(name);
      if (!view || view.hidden || !view.enabled || !view.batch_eligible) continue;
      const entry = state.selection.get(name);
      if (!entry) continue;
      entry.checked = Boolean(on);
      // ★ 2026-10-16：就地改勾选框，不整组重建。
      // 重建的代价有两个：把**别的**卡片上「停止执行」的按钮重建成「执行」
      // （闪烁 bug），以及把滚动位置弹回顶部。
      const card = cardElementFor(name);
      const box = card && card.querySelector(".task-actions--pick input[type=checkbox]");
      if (box) box.checked = Boolean(on);
    }
    saveSelection();
    refreshDerivedViews();
  }

  /**
   * 那份"两顿能不能吃"的缓存是否已经不可信（需要重发 32007/32008）。
   *
   * 【为什么要有失效时刻，而不是"读过就一直用"】
   * 窗口会翻篇：13:59 之后早宴席就不再是"开放中"、14:00 起换成晚宴席、
   * 06:00–06:10 两顿都不可吃。拿着早上的结论回答下午的问题，结果就是
   * "明明过了点却还提示能吃"。失效时刻由**服务端**算好
   * （``tasks/bbq.py::_expires_at_ms``，用的是它自己的毫秒时间戳 +
   * 固定的空档边界），前端只做一次比较 —— 不自己推窗口规则。
   *
   * 【服务端没给 expires_at 时按"不可信"处理】
   * 宁可多发一次包，也不要拿一份来历不明的状态去决定"要不要吃掉这一顿"。
   */
  function banquetCacheStale() {
    if (!Array.isArray(state.banquetMeals) || state.banquetMeals.length === 0) {
      return true;
    }
    if (!state.banquetExpiresAt) return true;
    return Date.now() >= state.banquetExpiresAt;
  }

  /**
   * 装填"两顿状态"并算出缓存有效期。
   *
   * 【为什么两处都要走它】
   * 数据有**两个来源**：``GET /api/banquet/status``（专门去问一次）与
   * 宴席任务跑完后步骤结果里自带的 ``meals``（就地更新，省一次 32007）。
   * 只更新 ``banquetMeals`` 而忘了 ``banquetExpiresAt``，那条路径拿到的
   * 数据就会被当成"永久有效" —— 这正是"过了点还说能吃"的来源。
   */
  function applyBanquetMeals(meals) {
    state.banquetMeals = Array.isArray(meals) ? meals : [];
    const stamps = state.banquetMeals
      .map((item) => Number(item.expires_at) || 0)
      .filter((value) => value > 0);
    // 服务端没给（旧后端）→ 记 0，``banquetCacheStale`` 按"不可信"处理
    state.banquetExpiresAt = stamps.length > 0 ? Math.min(...stamps) : 0;
    // ★ 2026-10-18：这里也把面板就地刷一遍 —— 数据来源不止 /api/banquet/status，
    // 宴席任务跑完的步骤结果里也带一份 meals（见 pollOnce）。以前那条路径只更新
    // 了 state，面板要等下一次重绘才变，看起来像"吃完了按钮还亮着"。
    syncBbqPanel(cardElementFor("bbq_energy"));
  }

  /**
   * 「一键执行」状态行。
   *
   * 回答三个用户真正关心的问题：**会跑哪些、为什么少跑了一个、现在能不能点**。
   */
  function renderManualBatchStatus() {
    const node = $("manual-batch-status");
    const button = $("btn-run-manual");
    if (!node || !button) return;
    node.className = "auto-status";

    const summary = batchSelectionSummary();
    const jobs = batchJobs(summary.runnable);
    // 待执行的任务名（宝箱不算"任务"；宴席两顿算一项 —— 用户视角它就是一张卡）
    const names = [];
    for (const job of jobs) {
      if (job.name === BOX_TASK) continue;
      if (!names.includes(job.name)) names.push(job.name);
    }

    if (state.running && state.batchRunId) {
      node.classList.add("auto-status--run");
      node.textContent = "主动任务：一键执行中…（点右侧「停止执行」可中断剩余任务）";
      button.textContent = "停止执行";
      button.classList.add("task-stop");
      button.disabled = false;
      return;
    }

    button.textContent = names.length > 0 ? `一键执行（${names.length}）` : "一键执行";
    button.classList.remove("task-stop");
    button.disabled = names.length === 0;

    const session = state.session || {};
    if (!session.has_session) {
      node.classList.add("auto-status--warn");
      node.textContent = "主动任务：需要先登录 —— 登录后可勾选任务批量执行。";
      return;
    }

    if (names.length > 0) {
      const mealCount = jobs.filter((job) => job.name === "bbq_energy").length;
      const suffix = mealCount > 0 ? `（含宴席 ${mealCount} 顿）` : "";
      const skipped =
        summary.done.length > 0
          ? `；${summary.done.map((name) => titleOf(name)).join("、")}本游戏日已完成，跳过`
          : "";
      node.classList.add("auto-status--ok");
      node.textContent =
        `主动任务：将执行 ${names.length} 个 —— ${names.map((name) => titleOf(name)).join("、")}` +
        `${suffix}${skipped}`;
      return;
    }

    if (summary.checked.length > 0) {
      // 勾了却一个都没进来，只有两种原因，逐条说清楚（含糊的"都没跑"会让人
      // 以为是勾选丢了或者工具坏了）。
      const reasons = [];
      if (summary.done.length > 0) {
        reasons.push(
          `${summary.done.map((name) => titleOf(name)).join("、")}本游戏日已完成`
        );
      }
      if (summary.checked.includes("bbq_energy")) {
        // 宴席不是 once_per_day（它的"吃过"记在 banquet_state 里），
        // 所以它进不来只可能是"此刻不可吃"或"时段还没读到"。
        reasons.push(
          state.banquetMeals
            ? "宴席本宴席日已吃过，或当前时段不可吃"
            : "宴席时段还没读到"
        );
      }
      node.classList.add("auto-status--warn");
      node.textContent = `主动任务：${reasons.join("；")}。`;
      return;
    }

    node.classList.add("auto-status--warn");
    node.textContent = "主动任务：未勾选任何任务。";
  }

  /**
   * 任务名 → ICON_PATHS 键。没登记的任务不画图标（保持纯文字，与从前一致）。
   *
   * ★ 2026-10-18 起值是**语义键**而不是 PNG 路径：同屏的指标卡与任务卡
   *   共用同一份图形（天梯竞技场 / 天命对决 / 失控炼成阵 / 主线关卡在两边
   *   都会出现），以后改图形只改 ICON_PATHS 一处。
   * ★ ``battle_experiment``（辉煌航迹试炼）从前借用 ``grand_line_supply``
   *   （船舵）的图，两者语义不同 —— 现在有独立的烧瓶 ``flask``。
   */
  const TASK_ICONS = {
    // 主动 / 自动任务
    mail: "mail",
    free_gift: "gift",
    friend_gift_claim: "gift_in",
    alliance_sign_in: "calendar_check",
    alliance_mining_personal: "pickaxe",
    alliance_mining_team: "users",
    alliance_donate: "coin_give",
    grand_line_supply: "compass",
    daily_box: "chest",
    sweep_activity: "map",
    sweep_material: "flame",
    market_buy: "bag",
    top_pvp_battle: "flag",
    battle_arena: "swords",
    wudou_pvp_battle: "hexstar",
    battle_experiment: "flask",
    battle_yimo: "skull",
    bbq_energy: "cutlery",
    buy_energy: "bolt_plus",
    buy_arena_ticket: "swords",
    buy_wudou_ticket: "hexstar",
    buy_top_pvp_ticket: "flag",
    push_main_stage: "stage",
    sign_in: "check_circle",
    daily_free_draw: "card_star",
    // 只读 / 状态类（2026-10-18 补齐）：同一玩法的"状态查询"与"开打"共用一个
    // 图形 —— 它们分属自动/主动两个分组，不会并排出现在同一屏，语义一致更好认。
    login: "switch_account",
    wallet: "wallet",
    roles: "roster",
    handshake: "plug",
    arena_status: "swords",
    wudou_status: "hexstar",
    yimo_status: "skull",
    top_pvp_status: "flag",
    main_stage_status: "stage",
    bbq_status: "cutlery",
    top_pvp_box: "chest",
    yimo_box: "chest",
    daily_task_auto: "checklist",
  };

  /**
   * 钻石使用页的任务视图。
   *
   * ★ 钻石使用板块现已改为全功能交互卡片（由 renderTaskGroup 渲染至 #tasks-diamond），
   * 直接在该板块中配置并执行带钻石消耗的任务。不再渲染只读摘要列表。
   * 保留此函数作为 no-op，确保历史调用点兼容。
   */
  function renderDiamondList() {
    // 钻石任务现由 renderTaskGroup("tasks-diamond", "diamond") 统一接管渲染与交互
  }

  function taskBadges(task) {
    const badges = [];
    if (!task.enabled) {
      badges.push(el("span", { class: "badge badge--danger", text: "未实现" }));
    } else if (!task.requires_auth) {
      badges.push(el("span", { class: "badge badge--ok", text: "免登录" }));
    }
    // 主动任务与钻石使用板块不展示"有花钱分支/可能花钻石/有消耗"红黄框提示（用户需求）
    if (task.group !== "manual" && task.group !== "diamond") {
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
   * 【主动任务为什么曾经没有复选框（用户 2026-09-24 要求）】
   * "勾选"的语义是"等着系统去做"，而主动任务需要的是"现在我明确要做"。
   * 用同一个控件表达两种相反的意图，误解的代价是真实的资源被花掉。
   *
   * 【★ 2026-10-16：主动任务右侧**又有**勾选框了 —— 但语义不同】
   * 它的含义是"纳入**一键执行**的候选集"，**不是**"等着系统自动跑"：
   * 勾上什么都不会发生，只有点顶部的「一键执行」才会发包（用户明确要求）。
   * 左边那个 ▸ 折叠箭头照旧（展开才看参数、才单独执行），两者互不干扰：
   * 勾选不展开参数区（``bodyOpen`` 没动），点勾选框也不会触发展开
   * （头部那个展开 handler 会跳过 ``input``）。
   * 只给 ``batch_eligible`` 的卡片画 —— 名单来自规格表，前端不抄。
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

  /**
   * 这张卡片**真正会画出来**的参数（★ 2026-10-18）。
   *
   * 【为什么需要它（这次修的 bug 的根）】
   * ``visibleParams`` 只回答"规格表里这个参数藏不藏"，不回答"渲染循环会不会跳过它"。
   * 宴席的 4 个参数（``meal`` / ``invite_*`` / ``invite_friends``）都被
   * :data:`BBQ_CUSTOM_PARAMS` 接管、在 ``paramsBoxFor`` 的循环里逐个 ``continue``，
   * 于是参数区是一个**空盒子**；而显隐判据用的是 ``visibleParams().length > 0``
   * （= 4），空盒子因此**始终可见**，自带 ``border-top + padding-top:10px``，
   * 与执行槽位那条分隔线一起演出"两块空白 + 两条细线"，还把展开时的高度顶大一截。
   *
   * 现在"会不会画出来"只有这一个答案：显隐、循环、``hasCandidates`` 全用它。
   */
  function drawnParams(task) {
    return visibleParams(task).filter(
      (param) => !(task.name === "bbq_energy" && BBQ_CUSTOM_PARAMS.has(param.name))
    );
  }

  /**
   * 参数区此刻要不要显示。
   *
   * 【为什么要单独一个函数】``paramsBoxFor``（首屏造节点）与
   * ``applyCardBodyOpen``（展开/折叠时就地改 class）必须用**同一套口径** ——
   * 以前两处各写一遍 ``bodyOpen && visibleParams().length > 0``，
   * 只要有一处忘了跟着改，就会出现"首屏是收的、点一下变开"的错位。
   *
   * ``push_main_stage`` / ``sweep_activity`` 的参数区里还塞了工具条
   * （进度横幅 / 「更新区域」），它们本身没有参数可言，所以要单独放行。
   */
  function paramsBoxShouldShow(task, bodyOpen) {
    if (!bodyOpen) return false;
    if (task.name === "push_main_stage" || task.name === "sweep_activity") return true;
    return drawnParams(task).length > 0;
  }

  /**
   * 造一张卡片的参数区。
   *
   * 【★ 2026-10-16：为什么它被抽出来】
   * 它现在有两个调用者：首屏渲染（``renderTask``）与"这一张卡片的参数选项变了"
   * （``refreshCard``）。以前这段只写在 renderTask 里，所以"下拉选项更新"
   * 就只能靠整组重建来解决。
   *
   * 【为什么参数区可以"始终构建 + hidden"，而执行控件不行】
   * 参数区里没有任何按 class 计数的验收判据（tools/verify_web_ui.py 只
   * print 一下它有没有 ``hidden``）；而 ``.task-exec`` 有"未展开时恰好 0 个"
   * 的硬判据 —— 所以只有执行控件必须真的不在 DOM 里（见 renderTask 的槽位）。
   */
  function paramsBoxFor(task, entry, bodyOpen) {
    const paramsBox = el("div", { class: "task-params" });
    paramsBox.classList.toggle("hidden", !paramsBoxShouldShow(task, bodyOpen));
    if (task.name === "push_main_stage") {
      paramsBox.appendChild(renderMainStageBanner());
    } else if (task.name === "sweep_activity") {
      // 「更新区域」收进展开栏（2026-10-03 用户要求）：标题行只留标题与说明
      paramsBox.appendChild(activityRefreshTools());
    } else if (task.name === "battle_arena") {
      paramsBox.appendChild(
        el("div", { class: "param" }, [
          el("div", { class: "param-tools" }, [
            iconSvg("shield", "stat-icon-svg", 14),
            el("span", { class: "badge badge--warn", text: "挑战限定" }),
            el("span", {
              class: "text-[12px] text-amber-200/90 leading-relaxed",
              text: "仅自动择弱挑选低战 NPC 训练家（战力 5 万以下）；若候选全为真人高战则安全跳过；对决后自动执行一键领奖；严禁买次数与天命系统。",
            }),
          ]),
        ])
      );
    } else if (task.name === "battle_yimo") {
      paramsBox.appendChild(
        el("div", { class: "param" }, [
          el("div", { class: "param-tools" }, [
            iconSvg("shield", "stat-icon-svg", 14),
            el("span", { class: "badge badge--ok", text: "挑战限定" }),
            el("span", {
              class: "text-[12px] text-emerald-200/90 leading-relaxed",
              text: "小伤害日常打卡模式：上报保底浮动得分（1200~2800），扣血万分比按「上报伤害 / 部位血量」折算（该得分下为 0），不改变全服世界 Boss 进度与排行榜。",
            }),
          ]),
        ])
      );
    }
    for (const param of drawnParams(task)) {
      paramsBox.appendChild(renderParam(entry, param, task.name));
    }
    return paramsBox;
  }

  function renderTask(task) {
    const entry = state.selection.get(task.name);
    const isExpandable = task.group === "manual" || task.group === "diamond";
    const expanded = isExpandable && state.expanded.has(task.name);
    const bodyOpen = isExpandable ? expanded : entry.checked;
    const shownParams = drawnParams(task);

    const paramsBox = paramsBoxFor(task, entry, bodyOpen);

    // ★ 2026-10-16：执行控件放进一个**固定槽位**，展开/折叠时只动槽位里的内容
    // （见 toggleCardExpanded / applyCardBodyOpen），不再整组重建整个列表。
    //
    // ⚠️ 折叠时槽位必须**整块不存在**（不是"存在但隐藏"），两个理由：
    //   ① 折叠态下 DOM 里一个 ``.task-exec`` 都不能有 —— tools/verify_web_ui.py
    //      是**按 class 全量计数、完全不看可见性**的，有一条判据就是
    //      "未展开时 .task-exec 恰好 0 个"、另一条是"展开后恰好 1 个"；
    //   ② 卡片是 ``grid gap-2.5``：多一个空 div 就多一条 10px 的网格间隙，
    //      每张折叠卡片都会凭空高 10px。
    // 所以这里与 applyCardBodyOpen 一起维护"有展开体才有槽位"这个不变量。
    //
    // ★ 2026-10-18：宴席槽位多一个 ``task-exec-slot--panel`` ——
    // 它装的是自带分隔线的专用面板（.bbq-meals），槽位不能再补一条分隔线
    // （否则就是截图里那"两条细线夹一块空白"）。见 input.css 的对应规则。
    const execSlot = el("div", {
      class:
        task.name === "bbq_energy"
          ? "task-exec-slot task-exec-slot--panel"
          : "task-exec-slot",
    });
    if (isExpandable && expanded) execSlot.append(...execControlsFor(task));

    const body = [paramsBox, execSlot];

    const hasCandidates = shownParams.some((param) => param.options_source);
    const headControl = isExpandable
      ? manualToggle(task, expanded)
      : autoControls(task, entry, hasCandidates);

    // 头部布局（2026-10-01）：主动任务/钻石任务 = ▸ 展开箭头在左（点卡片左边展开的
    // 肌肉记忆不变）；自动任务 = 信息靠左、拨动开关在最右。
    const info = el("div", { class: "task-info" }, [
      el("div", { class: "task-title" }, [
        iconSvg(TASK_ICONS[task.name], "task-icon-svg"),
        el("span", { text: task.title }),
        taskBadges(task),
      ]),
      task.summary ? el("p", { class: "task-sub", text: task.summary }) : null,
    ]);

    const head = el(
      "div",
      { class: `task-head ${isExpandable ? "task-head--manual" : "task-head--flip"}` },
      // ★ 2026-10-16：主动任务头部从两段变三段 —— 左边仍是展开箭头
      // （"点卡片左边展开"的肌肉记忆不变），右边多了「纳入一键执行」的勾选框。
      // 只给 ``batch_eligible`` 的卡片画：名单来自规格表，
      // 没登记的任务（购买体力 / 主线通关 / 钻石使用）连勾选框都没有 ——
      // 一个勾了却不生效的复选框比没有复选框更糟。
      isExpandable
        ? [headControl, info, manualPickControl(task, entry)]
        : [info, headControl]
    );
    if (isExpandable && task.enabled) {
      head.addEventListener("click", (event) => {
        if (event.target.closest("button, input, select, a, label")) return;
        toggleCardExpanded(task.name);
      });
    }

    return el(
      "div",
      {
        class: `task ${bodyOpen ? "task--on" : ""} ${expanded ? "task--expanded" : ""} ${
          task.enabled ? "" : "task--off"
        }`,
        // ★ 2026-10-16：卡片带上任务名。以前只有内部按钮带 data-task，
        // 于是"这张卡里的某个控件"只能靠位置去数（`querySelectorAll('.task-exec')[0]`）——
        // 而 tools/verify_web_ui.py 要点的恰恰是**指定任务**的控件
        // （一键执行那几条判据必须点确定的两个任务，否则结果随机）。
        "data-task": task.name,
      },
      [
        head,
        ...body,
      ]
    );
  }

  /**
   * 主动任务展开区里的执行控件：宴席卡是专用双按钮面板，其余是默认「执行」按钮。
   *
   * 收成一个函数是因为它有**两个调用者**：首屏渲染（``renderTask``）与展开/折叠
   * （``applyCardBodyOpen``）—— 两边必须造出同一种东西，否则"展开后"和"刷新后"
   * 会得到两种界面。
   */
  function execControlsFor(task) {
    return task.name === "bbq_energy" ? bbqMealControls(task) : manualExecControls(task);
  }

  /** 按任务名找卡片节点；名字来自规格表，但仍做一次转义兜底。 */
  function cardElementFor(taskName) {
    const escaped = String(taskName).replace(/["\\]/g, "\\$&");
    return document.querySelector(`.task[data-task="${escaped}"]`);
  }

  /**
   * 切换一张主动/钻石任务卡片的展开/折叠 —— **只动这一张卡片的 DOM**。
   *
   * 【★ 2026-10-16：为什么不再 renderTaskGroups()】
   * 以前展开一张卡会重建整个列表，代价有三个：
   *   ① **所有**卡片的执行按钮被重建 → 「停止执行」态丢失 → 就是用户看到的
   *      "正在执行/排队中的按钮闪一下变成没执行"（本次的 bug）；
   *   ② 正在填的参数输入框被替换掉（光标与内容丢失）、滚动位置弹回顶部；
   *   ③ 宴席面板被重绘（白闪一次）。
   * 展开/折叠本质上只是"这一张卡片多露一块内容"，所以只改这一张卡片。
   */
  function toggleCardExpanded(taskName) {
    const card = cardElementFor(taskName);
    const task = state.views.get(taskName);
    if (!card || !task) return;
    const opening = !state.expanded.has(taskName);
    if (opening) state.expanded.add(taskName);
    else state.expanded.delete(taskName);
    // ★ 展开宴席卡片时顺带确认一次两顿的实时窗口（否则按钮不知道能不能点）。
    // ⚠️ 只在这一个时机拉：32007/32008 是**风控可见**的流量，绝不能因为
    // "现在是局部更新了"就退化成每次重绘都发（判据是"已过期"，见 banquetCacheStale）。
    if (opening && taskName === "bbq_energy" && banquetCacheStale()) {
      refreshBanquetStatus(null);
    }
    applyCardBodyOpen(card, task, opening);
    saveExpandedPrefs();
  }

  /**
   * 把一张卡片的"展开体"刷成 ``bodyOpen`` 描述的样子（不重建卡片）。
   *
   * ``bodyOpen`` 的口径与 ``renderTask`` **完全一致**：
   * 主动/钻石任务 = 是否展开；自动任务 = 是否勾选（勾了才显示参数）。
   * 两种任务共用它，是为了让"参数区显隐"这条规则只写一份 —— 以前它只写在
   * ``renderTask`` 里，所以勾选框/箭头想改显隐就只能整组重建。
   */
  function applyCardBodyOpen(card, task, bodyOpen) {
    const isExpandable = task.group === "manual" || task.group === "diamond";
    card.classList.toggle("task--on", bodyOpen);
    card.classList.toggle("task--expanded", isExpandable && bodyOpen);
    const paramsBox = card.querySelector(".task-params");
    if (paramsBox) {
      paramsBox.classList.toggle("hidden", !paramsBoxShouldShow(task, bodyOpen));
    }
    const slot = card.querySelector(".task-exec-slot");
    if (slot) {
      // ⚠️ 先清空：折叠态下槽位必须真的空，一个 .task-exec 都不能留
      // （原因见 renderTask 里的注释：验收脚本按 class 全量计数）
      slot.textContent = "";
      if (isExpandable && bodyOpen) slot.append(...execControlsFor(task));
    }
    const toggle = card.querySelector(".task-toggle");
    if (toggle) {
      toggle.title = bodyOpen ? "收起这张卡片" : "展开：看参数并单独执行";
      toggle.setAttribute("aria-expanded", bodyOpen ? "true" : "false");
    }
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
    // ★ 2026-10-16：建按钮的那一刻就施加状态（同 manualExecControls）
    applyExecButtonState(execBtn, task.name);
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
      // ★ 2026-10-16：只改这一张卡片（卡片高亮 + 参数区显隐），其余视图局部刷新。
      // 以前这里整组重建 —— 顺带把**别的**卡片上「停止执行」的按钮重建成「执行」，
      // 就是用户看到的"执行中的按钮闪一下变回没执行"。
      refreshCard(task.name);
      refreshDerivedViews();
      // 勾选时若还没有候选（例如启动时未登录，或从未拉过），补一次自动拉取：
      // 命中缓存时它是零请求，只有真的没有数据才会联网。
      if (wanted && hasCandidates && activityOptions("activity_areas").length === 0) {
        await ensureActivityCache();
        // 候选到位后关卡下拉才有内容 —— 这一张卡片的参数区得重造
        refreshCard(task.name);
        refreshDerivedViews();
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
   * 【★ 2026-10-16：展开状态改成服务端持久化】
   * 以前它只存内存，刷新页面后一律折叠 —— 当初的理由是"免得上次点开的那张卡
   * 看起来像它还在执行/还在排队"。那条理由现在不成立了：一张卡有没有在执行，
   * 由按钮自己的「停止执行」红描边态如实表达（而且那个态是从服务端派生的），
   * 不再需要靠"卡片是不是展开的"去猜。所以展开状态跟 selection 一样
   * 服务端权威 + 本地兜底（见 saveExpandedPrefs / loadTasks）。
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
      // ★ 2026-10-16：只动这一张卡片（以前是整组重建，会让别的卡片的
      // 「停止执行」态丢失 —— 就是这次的闪烁 bug）
      toggleCardExpanded(task.name);
    });
    return button;
  }

  /**
   * 主动任务右侧的「纳入一键执行」勾选框（★ 2026-10-16 用户需求）。
   *
   * :return: 勾选框；**不参与一键执行的任务返回 ``null``**（不画任何东西）。
   *
   * 【与自动任务那个勾选框的关键差别】
   * 自动任务的 ``autoCheckbox`` 是"勾上就立刻跑一次"（勾选本身就是执行动作），
   * 所以它还会 ``runAutomation()``。这里**只记意愿**：真正跑不跑由用户点
   * 顶部「一键执行」决定 —— 主动任务要花资源，绝不能因为"顺手勾了一下"就发包。
   *
   * 【为什么不顺手把参数区展开】
   * 展开的卡片会跨双列铺满（``.task--expanded``），勾几个就把列表撑爆；
   * 而且"勾选"与"配置参数"是两件事，混在一起会让用户以为"勾了就等于配好了"。
   * 参数区仍然只由左边的箭头控制（见 ``bodyOpen``）。
   */
  function manualPickControl(task, entry) {
    if (!task.batch_eligible) return null;
    const checkbox = el("input", { type: "checkbox", class: "toggle" });
    checkbox.checked = Boolean(entry && entry.checked);
    checkbox.disabled = !task.enabled;
    checkbox.setAttribute("aria-label", `纳入一键执行：${task.title}`);
    checkbox.title = `纳入一键执行：${task.title}`;
    checkbox.addEventListener("change", () => {
      if (!entry) return;
      entry.checked = checkbox.checked;
      saveSelection();
      // ★ 2026-10-16：勾选只影响状态行里的"将执行 N 个"，局部刷新即可。
      // （它不改参数区显隐 —— 主动任务的展开只由左边的箭头控制，见 bodyOpen 的注释。）
      refreshDerivedViews();
    });
    return el("div", { class: "task-actions task-actions--pick" }, [checkbox]);
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

  /**
   * 宴席面板顶部那行状态文案（★ 2026-10-18）。
   *
   * 【它为什么单独成函数】
   * 这一行现在要同时表达三件事：**宴席日 / 换日时刻**（后端算好的）、
   * "时段读了没有"、以及"为什么现在读不了"（运行中）。
   * 而它有三个更新时机（首屏渲染、刷新成功、运行结束后自动补读），
   * 各写一遍文案必然长得不一样。
   *
   * 【为什么时间不在前端拼】
   * ``reset_text`` 来自 ``services/banquet_state.describe()``，
   * 也就是 ``daily_reset.BANQUET_RESET_HOUR/MINUTE``（可被环境变量覆盖）——
   * 前端写死 "06:10" 的话，游戏换点或改了环境变量之后界面就开始说谎，
   * 而且不会有任何报错。与"日常重置时间由后端给"是同一个原则。
   */
  function bbqMealsHeadText() {
    const banquet = state.banquet || {};
    const day = banquet.banquet_day || "";
    const resetText = banquet.reset_text || "";
    const live = Array.isArray(state.banquetMeals) && state.banquetMeals.length > 0;
    const dayText = day
      ? `宴席日 ${day}${resetText ? ` · ${resetText} 换日` : ""}`
      : "";
    if (live) return dayText || "宴席时段已读取";
    // ★ 2026-10-19：`hydrating` = 任务清单已在首帧画完、宴席/候选还在补。
    // 这时既没在飞也没欠账，说"尚未读取（点刷新）"是在冤枉自己。
    if (state.banquetLoading || state.hydrating) {
      return `${dayText ? `${dayText} · ` : ""}正在读取…`;
    }
    // 运行中读不了是**预期内**的事（/api/banquet/status 要抢运行闸门），
    // 文案要说清"不是坏了、也不用你反复点刷新"。
    if (state.banquetPending) {
      if (state.banquetPendingReason === "timeout") {
        return `${dayText ? `${dayText} · ` : ""}读取超时，稍后自动补读`;
      }
      return `${dayText ? `${dayText} · ` : ""}任务运行中，结束后自动读取`;
    }
    if (!dayText) return "尚未读取宴席时段（点「刷新」）";
    return `${dayText} · 时段未读取，点「刷新」`;
  }

  function bbqMealControls(task) {
    const entry = state.selection.get(task.name);
    const doneMeals = new Set(
      (state.banquet && state.banquet.meals) || []
    );
    const live = Array.isArray(state.banquetMeals) ? state.banquetMeals : null;

    const row = el("div", { class: "bbq-meals" });
    // 顶部一行：状态说明 + 「刷新」按钮（点它联网问一次两顿的实时窗口）
    const refresh = el("button", {
      class: "btn btn--ghost bbq-refresh",
      type: "button",
      text: "刷新",
      title: "联网读取两顿宴席的开放时间（任务运行中无法读取，会等它跑完自动读）",
    });
    refresh.addEventListener("click", async () => {
      await refreshBanquetStatus(refresh, { silent: false });
    });
    row.appendChild(
      el("div", { class: "bbq-meals-head" }, [
        el("span", { class: "bbq-meals-day", text: bbqMealsHeadText() }),
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
   * 单顿的**状态判定**（纯数据，不碰 DOM）。
   *
   * 【为什么要和 DOM 拆开（★ 2026-10-18）】
   * 同一套结论有两个消费场景：① 首屏/展开时"造节点"；② 刷新后"就地更新已有节点"。
   * 以前只有第 ① 条路，所以每次刷新都只能整块重建面板（白闪一次，用户点名要改）。
   * 拆成 view → apply 之后两条路共用同一份判定：规则只写一次，DOM 也不用重建。
   *
   * 【状态判定顺序】吃过 > 后端状态 > 未联网（``unknown``）。
   * 后端 ``state`` 已经是最终结论，这里只做"吃过"这一层的覆盖 ——
   * 记账在前端只可能更"晚"知道，不能更早。
   */
  function bbqMealView(meal, entry, { live, doneMeals }) {
    const label = meal === "morning" ? "早宴席" : "晚宴席";
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
      // 换日时刻由后端给（reset_text），不再写死 06:10 —— 见 bbqMealsHeadText。
      const resetText = (state.banquet && state.banquet.reset_text) || "";
      note = resetText ? `休整中 · ${resetText} 刷新` : "休整中";
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

    return {
      meal,
      label,
      stateKey,
      note,
      clickable,
      canInvite,
      // 补吃时在按钮上直接写明白，免得用户以为"这只和窗口内一样"
      buttonText: stateKey === "makeup" ? `${label} · 补吃` : label,
      inviteTitle: canInvite
        ? "这一顿邀请好友一起吃（花钻石）"
        : eaten
          ? "本宴席日已吃过"
          : "窗口外强制不邀请（后端也会拦）",
    };
  }

  /**
   * 把一份 :func:`bbqMealView` 的结果落到**已有**的面板节点上（就地更新）。
   *
   * 【为什么要遍历 BBQ_MEAL_STATE_CLASS 去 remove】
   * 状态类是一个"互斥集合"：面板上同一时刻只该挂一个 ``bbq-meal--*``。
   * 只加不删的话，``ready`` 转 ``eaten`` 时两条规则同时命中，谁生效取决于
   * CSS 里的先后顺序 —— 那种"颜色偶尔不对"的问题极难复现。
   */
  function applyBbqMealView(panel, view) {
    if (!panel || !view) return;
    const cls = BBQ_MEAL_STATE_CLASS[view.stateKey] || BBQ_MEAL_STATE_CLASS.unknown;
    const keys = Object.keys(BBQ_MEAL_STATE_CLASS);
    for (const key of keys) panel.classList.remove(BBQ_MEAL_STATE_CLASS[key].panel);
    panel.classList.add(cls.panel);

    const button = panel.querySelector(".bbq-meal-btn");
    if (button) {
      for (const key of keys) button.classList.remove(BBQ_MEAL_STATE_CLASS[key].button);
      button.classList.add(cls.button);
      if (button.textContent !== view.buttonText) button.textContent = view.buttonText;
      button.disabled = !view.clickable;
    }

    const note = panel.querySelector(".bbq-meal-note");
    if (note && note.textContent !== view.note) note.textContent = view.note;

    const inviteLabel = panel.querySelector(".bbq-meal-invite");
    if (inviteLabel) {
      inviteLabel.classList.toggle("bbq-meal-invite--locked", !view.canInvite);
      inviteLabel.title = view.inviteTitle;
      const toggle = inviteLabel.querySelector(".toggle");
      // ⚠️ 只改 disabled，**不碰 checked**：那是用户的选择，
      // 窗口短暂关闭不该把他的勾选悄悄抹掉（重新开放时还要用它）。
      if (toggle) toggle.disabled = !view.canInvite;
    }
  }

  /**
   * 就地把宴席卡片刷成当前 state（★ 2026-10-18：刷新不再整块重建）。
   *
   * :return: ``True`` = 找到并更新了面板；``False`` = 卡片/面板还不在 DOM 里
   *   （未展开、或宴席卡被过滤掉）—— 此时**什么都不用做**：
   *   下次展开时 ``bbqMealPanel`` 会按同一份 state 造出正确节点。
   */
  function syncBbqPanel(card) {
    if (!card) return false;
    const head = card.querySelector(".bbq-meals-day");
    if (!head) return false;
    // 只在文案真的变了才写：syncBbqPanel 会被**每次轮询**调到（5 秒一次），
    // 无脑赋同一个字符串也会替换文本节点、白白触发一次排版。
    const headText = bbqMealsHeadText();
    if (head.textContent !== headText) head.textContent = headText;
    const task = state.views.get("bbq_energy");
    const entry = state.selection.get("bbq_energy");
    if (!task || !entry) return false;
    const live = Array.isArray(state.banquetMeals) ? state.banquetMeals : null;
    const doneMeals = new Set((state.banquet && state.banquet.meals) || []);
    for (const meal of ["morning", "evening"]) {
      const panel = card.querySelector(`.bbq-meal[data-meal="${meal}"]`);
      if (!panel) continue;
      applyBbqMealView(panel, bbqMealView(meal, entry, { live, doneMeals }));
    }
    return true;
  }

  /** 单顿面板：标题 + 邀请开关 + 执行按钮（状态由 applyBbqMealView 落上去）。 */
  function bbqMealPanel(task, entry, meal, { live, doneMeals }) {
    const view = bbqMealView(meal, entry, { live, doneMeals });
    const inviteKey = meal === "morning" ? "invite_morning" : "invite_evening";
    const label = view.label;

    // 邀请开关（该顿独立）—— 窗口外直接置灰，不给人"点了再说"的机会
    const toggle = el("input", { type: "checkbox", class: "toggle" });
    toggle.checked = entry.params[inviteKey] === true || entry.params[inviteKey] === "true";
    toggle.addEventListener("change", () => {
      entry.params[inviteKey] = toggle.checked;
      saveSelection();
    });

    const inviteLabel = el(
      "label",
      { class: "bbq-meal-invite" },
      [toggle, el("span", { text: "邀请好友" })]
    );

    const button = el("button", {
      class: "btn btn--primary bbq-meal-btn",
      type: "button",
      "data-task": task.name,
      "data-meal": meal,
    });
    // 点了就按"这一顿 + 该顿邀请开关"提交一次运行
    button.addEventListener("click", () => onBbqMealClick(task, meal, button));

    const panel = el("div", { class: "bbq-meal", "data-meal": meal }, [
      el("div", { class: "bbq-meal-top" }, [
        el("span", { class: "bbq-meal-name", text: label }),
        inviteLabel,
      ]),
      button,
      el("em", { class: "bbq-meal-note", text: view.note }),
    ]);
    // 状态类、按钮文案、置灰、状态注一次到位（与就地更新走同一条路径）
    applyBbqMealView(panel, view);
    return panel;
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
        state.optimisticRuns.add(task.name);
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
   *
   * 【★ 2026-10-16：它的时机变了】
   * 以前只有"展开宴席卡片且还没读过"才会拉一次，于是用户的实际体验是
   * "每次都要手动点一下「刷新」"。现在改成**进主界面就自动拉一次**
   * （首屏加载 / 登录成功 / 切区后跳回本页都走 ``boot → loadTasks``，
   * 见 ``ensureBanquetStatus``），面板上那个「刷新」按钮降级为
   * "我想立刻再确认一次"的手动入口。
   *
   * 【为什么必须记下 expires_at】
   * 窗口会翻篇（13:59 早宴席结束、14:00 换晚宴席、06:00–06:10 空档）。
   * 服务端在每顿状态里给了 ``expires_at``（它算好"下一个会让结论变化的时刻"），
   * 这里取最小值存下来，超时就作废重拉 —— 否则会拿过期窗口判断"能不能吃"。
   *
   * 【★ 2026-10-18：三处用户反馈的落点】
   * ① **"上一次运行还没有结束"不该是红色报错** ——
   *    ``/api/banquet/status`` 走的是 ``run_inline``，必须抢全局运行闸门
   *    （``webapi/state.py``），自动任务在跑时必然 409。那是**预期内**的事，
   *    不是故障：现在**一条日志都不写**，只记下"欠一次"，等闸门空了静默补读。
   * ② **连点 / 多个触发点不该各发一次** —— ``state.banquetLoading`` 做并发锁；
   *    失败后靠 ``banquetRetryAt`` 指数退避（见 retryBanquetAfterRun）。
   * ③ **刷新不该把面板整块重建**（白闪）—— 成功后只调 ``syncBbqPanel`` 就地更新。
   */
  //: 宴席补读的退避参数（★ 2026-10-18 用户要求"自动获取不弹日志"）。
  //: 失败不写日志、只静默退避；最多 4 次（5→15→45→60 秒），
  //: 之后交回"下次展开 / 刷新 / 启动"重新触发。
  const BANQUET_RETRY_BASE_MS = 5000;
  const BANQUET_RETRY_MAX_MS = 60000;
  const BANQUET_RETRY_MAX_TIMES = 4;
  // ★ 2026-10-19：只读查询的客户端超时。只给"会挡住首屏 / 启动链"的三处用
  // （宴席时段、活动候选、素材目录，见 notes/task_list_first_paint_plan.md）——
  // 不做全局默认值：运行日志轮询等请求有自己的节奏，不该被统一掐断。
  // 8 秒的选择：实测 32007/32008 往返 ~2s，活动缓存冷启动 ~3s，
  // 留一倍余量；真挂住时宁可当作"读失败、稍后补读"，也不能一直转圈。
  const READ_TIMEOUT_MS = 8000;

  async function refreshBanquetStatus(button, { silent = true } = {}) {
    // 并发锁：开机自动拉、展开卡片、用户点「刷新」可能同时发生 ——
    // 以前它们各发一个请求，前两个撞上运行闸门就各写一条红叉。
    if (state.banquetLoading) return null;
    state.banquetLoading = true;
    setBbqRefreshBusy(button, true);
    const card = cardElementFor("bbq_energy");
    syncBbqPanel(card); // 立刻把"正在读取…"显示出来
    try {
      const payload = await call("/api/banquet/status", { timeout: READ_TIMEOUT_MS });
      applyBanquetMeals(payload.meals);
      if (payload.banquet) state.banquet = payload.banquet;
      state.banquetPending = false;
      state.banquetPendingReason = "";
      state.banquetRetryAt = 0;
      state.banquetRetryDelay = BANQUET_RETRY_BASE_MS;
      state.banquetRetryCount = 0;
      syncBbqPanel(card);
      refreshDerivedViews();
      return payload;
    } catch (error) {
      if (isRunBusy(error) || isTimeout(error)) {
        // 运行 / 只读快照占着闸门 = **预期内**；读超时同理 ——
        // 两者都只记"欠一次"，由 pollOnce 静默退避补读，绝不写日志
        // （用户 2026-10-18 要求：自动获取不许弹提示）。
        state.banquetPending = true;
        state.banquetPendingReason = isTimeout(error) ? "timeout" : "busy";
        state.banquetRetryAt =
          Date.now() + (state.banquetRetryDelay || BANQUET_RETRY_BASE_MS);
        syncBbqPanel(card);
        return null;
      }
      // 真正的失败（网络 / 令牌 / 后端 bug）：自动路径静默，手动「刷新」才如实报
      state.banquetPending = false;
      state.banquetPendingReason = "";
      state.banquetRetryDelay = BANQUET_RETRY_BASE_MS;
      state.banquetRetryCount = 0;
      if (!silent) appendRun("✘", `读取宴席时段失败：${describeError(error)}`, "ERROR");
      return null;
    } finally {
      state.banquetLoading = false;
      setBbqRefreshBusy(button, false);
      syncBbqPanel(cardElementFor("bbq_energy"));
    }
  }

  /** 409 = 运行闸门被占（不是错误，只是"现在不行"）。 */
  function isRunBusy(error) {
    return Boolean(error && error.status === 409);
  }

  /** 读超时（`common.js::call` 的 AbortController 标记）：也按"稍后再读"处理。 */
  function isTimeout(error) {
    return Boolean(error && error.timeout);
  }

  /** 「刷新」按钮的忙碌态（标题不变：它是常驻说明，不是临时提示）。 */
  function setBbqRefreshBusy(button, busy) {
    const btn = button || (() => {
      const card = cardElementFor("bbq_energy");
      return card ? card.querySelector(".bbq-refresh") : null;
    })();
    if (!btn) return;
    btn.disabled = busy;
    const text = busy ? "读取中…" : "刷新";
    if (btn.textContent !== text) btn.textContent = text;
  }

  /**
   * 需要时就把宴席时段拉一次（登录 / 切区 / 首屏都会调它）。
   *
   * 【为什么单独包一层】
   * 三个触发点要的是同一件事："现在没有可信数据就去拿一份"。写成三处
   * ``if (banquetCacheStale()) refreshBanquetStatus(null)`` 的话，
   * 将来加了新的失效条件（比如服务端多给一个字段）必然漏改一处。
   */
  async function ensureBanquetStatus() {
    const session = state.session || {};
    if (!session.has_session) return null;
    if (!banquetCacheStale()) return null;
    return refreshBanquetStatus(null);
  }

  /**
   * 闸门空了就补读一次宴席时段（★ 2026-10-18，2026-10-18 晚重写）。
   *
   * 【为什么要专门一个入口】
   * 运行中拉不到是必然的，但不该让用户"等下次运行完再手动点刷新" ——
   * 用户对这张卡的期待是"打开就知道现在能不能吃"。所以把那次 409 记成
   * ``banquetPending``，在轮询看到 **运行已结束** 时补一次。
   *
   * 【★ 2026-10-18 晚：为什么重写（用户报"每 5 秒弹一条"）】
   * 旧实现发请求**之前**就把 ``banquetPending`` 清了。于是 409 分支里的
   * "是不是第一次"判据永远成立，每轮都写一条 ⏳ —— 用户看到的就是 5 秒一条。
   * 现在：
   *   1. ``pending`` **只在成功时**清（见 ``refreshBanquetStatus``）；
   *   2. 失败按 ``5→15→45→60`` 秒退避，最多 4 次，之后静默放弃；
   *   3. **一条日志都不写**（用户要求：自动获取不弹提示）；
   *   4. 只读快照（``snapshotBusyUntil``）在飞时直接跳过，不去撞同一把闸门。
   */
  function retryBanquetAfterRun() {
    if (state.running) return;
    if (!state.banquetPending) return;
    if (Date.now() < (state.banquetRetryAt || 0)) return;
    // 信息面板快照 / 主线状态还占着闸门 → 这轮不试
    if (Date.now() < (state.snapshotBusyUntil || 0)) return;
    if (!banquetCacheStale()) {
      state.banquetPending = false;
      state.banquetPendingReason = "";
      return;
    }
    if (state.banquetRetryCount >= BANQUET_RETRY_MAX_TIMES) {
      // 试够了：静默放弃，交给"下次展开 / 刷新 / 启动"重新触发
      state.banquetPending = false;
      state.banquetPendingReason = "";
      return;
    }
    state.banquetRetryCount += 1;
    state.banquetRetryAt = Date.now() + state.banquetRetryDelay;
    state.banquetRetryDelay = Math.min(
      state.banquetRetryDelay * 3,
      BANQUET_RETRY_MAX_MS
    );
    ensureBanquetStatus();
  }

  /**
   * 主动任务（非宴席）的默认操作区：一个「执行」按钮（+ 可能的"本日已完成"说明）。
   *
   * 宴席改用专用双按钮面板（见 :func:`bbqMealControls`），不走这里。
   *
   * 【★ 2026-10-16：本游戏日已做过的任务要置灰】
   * 用户对工会捐献的要求是"和吃宴席一样，执行一次后就变灰"。判据直接用
   * 服务端下发的账本 ``state.autoDone``（= ``/api/tasks`` 的 ``auto_done``，
   * 由 ``webapi/state.py`` 在任务**成功**后写入），前端不自己记 ——
   * 本地记的账换个端口/换个壳就丢了（与 autoDone 判重同一个教训）。
   * 置灰只挡「执行」按钮，**不动勾选框**：勾选表达的是"下次一键带上我"，
   * 与"今天已经做过了"是两件事。
   */
  function manualExecControls(task) {
    const doneToday = Boolean(task.once_per_day && state.autoDone.has(task.name));
    const button = el("button", {
      class: "btn btn--primary task-exec",
      type: "button",
      text: execLabel(),
      "data-task": task.name,
    });
    // ★ 2026-10-04：执行中**不再禁用** —— 点了就排队（见 onExecClick）。
    // ★ 2026-10-16：它已经在跑/在排队时也**不禁用** —— 那时按钮是「停止执行」，
    // 一个置灰的"停止"等于不让人反悔。
    button.disabled = !task.enabled || (doneToday && !isTaskStoppable(task.name));
    if (doneToday) button.title = "本游戏日已完成，明天 05:00 后恢复";
    // ★ 2026-10-16：建按钮的那一刻就施加状态（这是"展开不闪"的关键一步）
    applyExecButtonState(button, task.name);
    button.addEventListener("click", () => onExecClick(task, button));
    if (!doneToday) return [button];
    return [
      button,
      el("p", {
        class: "task-note",
        text: "本游戏日已完成（明天 05:00 后恢复）—— 一键执行也会自动跳过它。",
      }),
    ];
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
   * 这个任务此刻是不是"在跑 / 还在排队"（= 按钮该显示「停止执行」）。
   *
   * 【★ 2026-10-16：为什么必须收敛成一个判定】
   * 判定以前散在两处，而**渲染按钮的那两个函数一次都不读** —— 于是任何
   * "整组重建"（展开一张卡片、勾选、改参数……）都会把按钮重建成常态「执行」，
   * 要等下一次轮询（运行中 1.2s / 空闲 5s）才变回来。用户看到的就是
   * "正在执行的按钮闪一下变成没执行"。现在判定只有这一份，而且**渲染时也要
   * 施加**（见 applyExecButtonState），所以"重建丢状态"这类 bug 从结构上没有了。
   */
  function isTaskStoppable(name) {
    return state.activeTasks.has(name) || state.optimisticRuns.has(name);
  }

  /**
   * 把一个执行按钮刷成它此刻该有的样子（文案 + 配色 + 提示）。
   *
   * 【它有两个调用者，缺一不可】
   *   ① 建按钮时（manualExecControls / autoControls）—— 保证**新造的**按钮
   *      生下来就是对的。这是"重建不丢状态"的关键，也是 2026-10-16 那次
   *      「展开卡片导致按钮闪烁」的真正修复点。
   *   ② syncExecButtons() 遍历时 —— 保证**已经存在的**按钮随轮询原地变，
   *      不必重建 DOM（重建会打断正在填的参数、把滚动弹回顶部）。
   * 两条路径共用同一份规则，才不会出现"渲染出来是一种、轮询后是另一种"。
   *
   * 原始 title 记在 ``data-exec-title`` 上：``.task-auto-exec`` 生来就带一句
   * "主动执行「XXX」"，切到停止态再切回来时必须还原成它，而不是留一句
   * "中断正在执行的任务"（旧代码的 ``: button.title`` 是个自赋值空操作）。
   */
  function applyExecButtonState(button, name) {
    if (!button) return;
    if (!button.dataset.execTitle) {
      button.dataset.execTitle = button.title || "";
    }
    const stopping = isTaskStoppable(name);
    button.classList.toggle("task-stop", stopping);
    button.textContent = stopping ? "停止执行" : execLabel();
    button.title = stopping ? "中断正在执行的任务" : button.dataset.execTitle;
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
    if (isTaskStoppable(task.name)) {
      stopCurrentRun(task);
      return;
    }
    if (task.group === "diamond") runDiamondTask(task, button);
    else if (task.group === "manual") runManualTask(task, button);
    else runSingleAutoTask(task, button);
  }

  /**
   * 让界面上所有执行按钮的**文案与配色**跟状态对齐（不重建 DOM）。
   *
   * 【为什么只改属性而不 renderTaskGroups()】
   * 轮询 1.2 秒一次；整组重建会打断正在填的参数、把滚动位置弹回顶部。
   * 按钮只有文字与一个类名会变，原地改最省事也更稳。
   *
   * 【★ 2026-10-16：判定与施加都交给 applyExecButtonState】
   * 这里不再自己写 ``state.stopTasks.has(...)`` —— 那份判定现在和
   * "建按钮时"共用同一个函数，才不会出现两条路径不同步。
   */
  function syncExecButtons() {
    for (const button of document.querySelectorAll(".task-exec, .task-auto-exec")) {
      const name = button.getAttribute("data-task");
      if (!name) continue;
      applyExecButtonState(button, name);
    }
  }

  /**
   * 中断**这一张卡片对应的那一批**（队列里剩下的照常跑）。
   *
   * 【★ 2026-10-16：改成按 run_id 精确停】
   * 以前这里发的是**空 body**，服务端的 ``stop()`` 会退化成"停当前正在跑的那一批"
   * （见 ``webapi/state.py``）。于是出现"按钮说停 A、实际被停的是正在跑的 B"——
   * 你点的那张卡很可能还在排队，被误停的却是别人的任务。
   * run_id 从轮询派生的 ``activeRunIds`` 里取（见 pollOnce），取不到才退回老语义
   * （空 body）—— 老服务端 / 旧数据下依旧能用。
   *
   * 【为什么成功后要往运行结果里写一行】
   * 服务端的日志要经过轮询才会到界面上，中间有 1 秒左右的空窗。
   * 用户点了停止却"什么都没发生"，第一反应一定是"按钮坏了" ——
   * 所以这里立刻补一行本地结果，点下去就有反馈。
   * 排队中的批次服务端会回 ``cancelled_queued``，措辞与一键执行的
   * ``stopManualBatch`` 保持一致，用户不必学两套说法。
   */
  async function stopCurrentRun(task) {
    const runId = state.activeRunIds.get(task.name) || null;
    try {
      const payload = await call("/api/stop", {
        method: "POST",
        body: runId ? { run_id: runId } : {},
      });
      if (payload && payload.stopped) {
        const queued = payload.cancelled_queued ? "（它还在排队，已直接撤销）" : "";
        appendRun("⏹", `已请求中断：${task.title}${queued}`, "warn");
      } else {
        appendRun("⚠", "当前没有可中断的运行。", "warn");
        // ★ 2026-10-16：回滚本地乐观值。服务端说"没得停"，说明我们以为在跑的
        // 那件事其实已经结束了 —— 三个记账都得摘掉，否则按钮会一直显示
        // 「停止执行」却点不动。
        state.optimisticRuns.delete(task.name);
        state.activeTasks.delete(task.name);
        state.activeRunIds.delete(task.name);
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
      const payload = await call("/api/material/tree", {
        params: { category: "all" },
        // 本机读配置通常几十毫秒；给超时只为"磁盘/进程异常时别把首屏链挂死"。
        timeout: READ_TIMEOUT_MS,
      });
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
    // 8 秒超时：这个接口在缓存过期时会真的联网（11025），必须给它一个上界，
    // 否则 boot 的补水链 / 启动自动任务会被一次卡死的请求无限期拖住。
    return call("/api/activity/preview", { params, timeout: READ_TIMEOUT_MS });
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
    // ★ 2026-10-18：下限由**规格表**下发（`param.minimum`）——
    // 「钻石使用」的四个购买次数是 1（取消 0 次 / 不再提供只读），
    // 其余数字参数（含荣耀之巅 battles）没有这个字段，缺省 0 语义一字不变。
    // 这里刻意不按任务名猜：参数面的唯一来源是后端的规格表。
    const rawMin = Number(param.minimum);
    const lowerBound = Number.isFinite(rawMin) ? rawMin : 0;
    const current = entry.params[param.name];

    // 存着的旧值低于下限（最典型：以前「购买次数」选了 0 = 只读）→ 打开页面
    // 就归位到下限并写回。否则界面显示 1、提交的却还是 0，后端会直接拒绝，
    // 表现成"看着填好了，点执行却报错"。
    let initial = current;
    if (current === null || current === undefined || current === "") {
      // 下限 > 0 时不留空：空着提交会落到 default，界面与请求就对不上了。
      initial = lowerBound > 0 ? lowerBound : "";
    } else {
      const numeric = Number(current);
      if (Number.isFinite(numeric) && numeric < lowerBound) initial = lowerBound;
    }
    if (initial !== current) {
      entry.params[param.name] = initial;
      saveSelection();
    }

    const input = el("input", {
      type: "number",
      min: String(lowerBound),
      step: String(step),
      inputmode: isFloat ? "decimal" : "numeric",
      class: "stepper-input",
    });
    input.value = initial === "" ? "" : String(initial);

    /** 减号在下限处禁用：点得动却没反应，比明显按不了更让人困惑。 */
    function syncMinusState() {
      const text = String(input.value).trim();
      minus.disabled = text !== "" && Number(text) <= lowerBound;
    }

    /** 把"算好的数值"写回参数并持久化（数字用数字存，后端会照 kind 再转一次）。 */
    function commit(value) {
      input.value = String(value);
      entry.params[param.name] = value;
      saveSelection();
      renderDiamondList();
      syncMinusState();
    }

    /** 按步长加减：空值 / 非法值按 default → 下限处理，绝不越过下限。 */
    function nudge(direction) {
      const typed = Number(input.value);
      const fallback = Number(param.default);
      const base = Number.isFinite(typed)
        ? typed
        : Number.isFinite(fallback)
          ? fallback
          : lowerBound;
      const raw = base + direction * step;
      const clamped = Math.max(lowerBound, raw);
      commit(isFloat ? Math.round(clamped * 100) / 100 : Math.round(clamped));
    }

    input.addEventListener("input", () => {
      // 输入过程中不纠正（否则删到一半就被改成 0，没法输入新数字）
      entry.params[param.name] = input.value;
      saveSelection();
      syncMinusState();
    });
    input.addEventListener("blur", () => {
      const raw = String(input.value).trim();
      if (raw === "") {
        // 空 = "没填"（例如荣耀之巅的 battles 空着就是用配置默认值），保持空；
        // 但下限 > 0 的参数不允许空着 —— 直接落回下限（购买次数 = 1）。
        if (lowerBound > 0) {
          commit(lowerBound);
          return;
        }
        entry.params[param.name] = "";
        saveSelection();
        syncMinusState();
        return;
      }
      let value = Number(raw);
      if (!Number.isFinite(value) || value < lowerBound) value = lowerBound;
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
    syncMinusState();

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

  function renderChoiceParam(entry, param, taskName) {
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
          // ★ 2026-10-16：只重建这一张卡片的参数区（换大类会连带清掉不匹配的
          // 区域/关卡）。以前是整组重建 —— 顺带把所有卡片的「停止执行」抹掉。
          refreshCard(taskName);
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
  function renderCandidateParam(entry, param, taskName) {
    const source = param.options_source;
    const current = entry.params[param.name];
    const known = candidateOptions(entry, source);

    /** 下拉里的一项（``disabled`` 的选项只是"说明文字"，点不动）。 */
    const options = [];
    let selectDisabled = false;

    if (known.length === 0) {
      let emptyText = "（尚无候选）";
      // ★ 2026-10-19：首帧先出清单、候选随后补（见 loadTasks）。这段窗口里
      // 说"请先登录 / 点更新区域"会让人以为读失败了 —— 如实写"正在读取"，
      // 补水完成后 refreshHydratedCards() 会重画这张卡。
      if (state.hydrating) {
        emptyText = "（正在读取…）";
      } else if (source === "activity_areas") {
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
            refreshCard(taskName);
          } else {
            state.activityOptions.activity_sections = [];
            state.activityBlocked = [];
            clearStaleSectionSelections();
            refreshCard(taskName);
            // 异步兜底拉关卡
            void loadSectionsForArea(value).then(() => refreshCard(taskName));
          }
        } else if (source === "material_areas") {
          // 换素材区域 → 清空上一区的指定关卡，并重新渲染刷新关卡下拉列表
          entry.params.section = "";
          saveSelection();
          refreshCard(taskName);
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

  function renderParam(entry, param, taskName) {
    const current = entry.params[param.name];
    let input;

    // ★ 这两个分支必须排在 ``kind`` 判断**之前**：``choices`` / ``options_source``
    // 描述的是"怎么渲染"，而 ``kind`` 只说值是什么类型（times 是 int，界面只给三档）。
    if (Array.isArray(param.choices) && param.choices.length > 0) {
      return renderChoiceParam(entry, param, taskName);
    }
    if (param.options_source) {
      return renderCandidateParam(entry, param, taskName);
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
   * 按分组过滤比在每个调用点写 if 更不容易出错。
   *
   * 【★ 2026-10-16：修正一句过时的说明】
   * 这里原先写着"勾选这个标记现在**只对自动任务有意义**（主动任务没有复选框了）"。
   * 那句从 2026-10-16 起不成立了：主动任务卡片右侧也有了勾选框
   * （见 ``manualPickControl``），语义是"纳入一键执行"。
   * 不过**一键执行不走这个函数** —— 它要的是一份**有序**清单
   * （服务端 ``batch_tasks`` 给的顺序）+ 每个任务后跟一个宝箱 + 宴席两顿拆项
   * + 跳过今天已做过的，见 ``batchSelectionSummary`` / ``batchJobs``。
   * 把那些规则塞进这个通用函数，只会让"自动执行"这条路径也背上它们。
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
      // 队列跑空：所有"停止执行"复原成"执行"。
      // ★ 2026-10-16：``activeTasks`` 本来就已经由轮询按服务端事实重算过
      // （跑空时 steps 里只剩已完成批，派生出空集）；这里再显式清一遍是
      // **防御性**的 —— 万一将来有人把 setRunning 调在这个顺序之外。
      state.activeTasks.clear();
      state.activeRunIds.clear();
      state.optimisticRuns.clear();
      renderTaskGroups();
    } else {
      syncExecButtons();
      renderAutoStatus();
      // ★ 2026-10-16：一键执行的按钮要在「一键执行 ↔ 停止执行」之间跟着切，
      // 而这条路径不会整组重建卡片（那会打断用户正在填的参数）。
      renderManualBatchStatus();
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
      // ★ 2026-10-16：把批次号留给调用方。一键执行要用它做"精确停止"
      // （那批可能还在排队，而 /api/stop 默认停的是"正在跑的那一批"）。
      // 注意别写成 state.lastRunId —— 那个是结果流水的去重游标。
      state.submittedRunId = result.run_id || null;
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
   * 执行一个钻石使用任务（用户在「钻石使用」板块点开卡片后按「执行」）。
   *
   * 1. 前置安全闸门：检查钻石总闸（#opt-allow-diamond），未开启时友好阻断并提示；
   * 2. 仅提交该任务本身，不附加活跃宝箱；
   * 3. 提交至队列执行，支持通过「停止执行」中断。
   */
  async function runDiamondTask(task, button) {
    const allowDiamond = Boolean($("opt-allow-diamond") && $("opt-allow-diamond").checked);
    if (!allowDiamond) {
      appendRun("⚠", `执行「${task.title}」被阻止：钻石总闸未开启，请先在上方勾选「允许花钻石（总闸）」`, "warn");
      if (window.Ct && typeof window.Ct.uiAlert === "function") {
        window.Ct.uiAlert(`执行「${task.title}」需要消耗钻石，请先在上方开启「允许花钻石（总闸）」。`);
      }
      return;
    }
    const entry = state.selection.get(task.name);
    if (!entry) return;

    const jobs = [{ name: task.name, params: { ...entry.params } }];

    try {
      const result = await submitRun(jobs, { reason: `钻石任务：${task.title}` });
      if (result === "ok") {
        state.optimisticRuns.add(task.name);
        if (button) button.disabled = false;
        syncExecButtons();
      }
    } finally {
      if (button) button.disabled = !task.enabled;
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
        state.optimisticRuns.add(task.name);
        if (button) button.disabled = false;
        syncExecButtons();
      }
    } finally {
      // 失败时按钮保持可用（不再由"运行中"禁用），跑完由 setRunning 复原
      if (button) button.disabled = !task.enabled;
    }
  }

  /**
   * 「一键执行」：把勾上的主动任务按顺序**一次**提交（★ 2026-10-16 用户需求）。
   *
   * 【为什么必须是一次 /api/run，而不是"逐个点执行按钮"】
   * 两者跑出来的任务清单、顺序、每个任务后的宝箱完全一样；差别只在
   * **能不能整批停**：服务端的 ``/api/stop`` 只中断"当前这一批"，
   * 队列里其他批次照跑。分成 N 批的话，用户点「停止执行」只能停掉手上
   * 那一个任务，后面 5 批照跑 —— 而他要的是"从当前任务开始，后面全都别跑"。
   *
   * 【为什么不弹二次确认（2026-10-02 起主动任务就没有了）】
   * 勾选本身就是一次显式动作，再叠一层"确定吗"只会让人养成闭眼点确定的习惯。
   * 消耗的告知由卡片上的「⚠ 消耗资源」徽标、参数的 danger 标注、
   * 以及钻石总闸（真正的硬闸）共同承担。
   *
   * 【宴席的特殊处理】
   * 它一天两顿、每顿一项（早在前），且**不串宝箱**（与专用按钮语义一致）；
   * 只有"没吃过且此刻能吃"的顿才会进列表（见 ``batchBanquetMeals``）。
   */
  async function runManualBatch() {
    const session = state.session || {};
    if (!session.has_session) {
      uiAlert("请先登录，再使用一键执行。");
      return;
    }

    // 宴席的"能不能吃"要靠 32007/32008 的窗口数据。缓存过期/还没读过就先补一次，
    // 补不到也不阻断整批 —— 只是把宴席摘掉（其余任务照跑），并在状态行说明。
    const bbqPicked = batchSelectionSummary().runnable.includes("bbq_energy");
    if (bbqPicked && banquetCacheStale()) {
      await refreshBanquetStatus(null);
    }

    const summary = batchSelectionSummary();
    const jobs = batchJobs(summary.runnable);
    if (jobs.length === 0) {
      uiAlert("没有可执行的任务：请先勾选，或确认勾选的任务本日还没做过。");
      renderManualBatchStatus();
      return;
    }

    const names = [];
    for (const job of jobs) {
      if (job.name === BOX_TASK) continue;
      if (!names.includes(job.name)) names.push(job.name);
    }

    const result = await submitRun(jobs, {
      reason: `主动任务一键执行（${names.length} 个）`,
    });
    if (result === "ok") {
      // 记住批次号：让「停止执行」精确撤掉**这一批**（它可能还在排队，
      // 而服务端默认停的是"正在跑的那一批" —— 那就停错对象了）。
      state.batchRunId = state.submittedRunId;
      // 这批还没跑完 → 相关卡片的按钮也变成「停止执行」，点哪个都停这一批
      for (const name of names) state.optimisticRuns.add(name);
      syncExecButtons();
    }
    renderManualBatchStatus();
  }

  /**
   * 「停止执行」（一键执行那一排的按钮）。
   *
   * 语义 = **从当前正在执行的任务开始，之后的所有任务都不执行**
   * （用户 2026-10-16 原话）。服务端 :meth:`stop` 本来就满足这一点
   * （当前步骤在任务边界停下、其余 pending 标成 skipped），
   * 我们额外做的是**认人**：带上 ``run_id``，只撤自己提交的那一批。
   */
  async function stopManualBatch() {
    const runId = state.batchRunId;
    try {
      const payload = await call("/api/stop", {
        method: "POST",
        body: runId ? { run_id: runId } : {},
      });
      if (payload && payload.stopped) {
        const queued = payload.cancelled_queued ? "（它还在排队，已直接撤销）" : "";
        appendRun("⏹", `已请求中断一键执行${queued}`, "warn");
      } else {
        appendRun("⚠", (payload && payload.message) || "当前没有可中断的运行。", "warn");
      }
    } catch (error) {
      appendRun("✘", `中断失败：${describeError(error)}`, "ERROR");
    } finally {
      state.batchRunId = null;
      renderManualBatchStatus();
      syncExecButtons();
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
        state.optimisticRuns.add(task.name);
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
    // ★ 2026-10-18：逐场 / 逐关的进度（``PROGRESS`` 级别，见 services/progress.py）
    // 直接进「运行结果」流水 —— 这正是用户要的"打完一场就输出一场，最后再总结"。
    // 任务结束那行 ✔/✘ 步骤结果照旧是收尾的总结，两者不在同一个来源、不会重复。
    if (line.level === "PROGRESS") {
      appendRun(
        "⚔",
        String(line.message).split("\n")[0],
        "PROGRESS",
        { countUnread, at: line.ts || "" }
      );
      return;
    }
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
      if (status.banquet) {
        state.banquet = status.banquet;
        // ★ 2026-10-18：宴席日刚翻篇时（06:10 之后），面板头部那行
        // "宴席日 … · 06:10 换日"与两顿的置灰状态都要跟着变 ——
        // 就地刷，不重建（重建会白闪，且会打断正在点的按钮）。
        syncBbqPanel(cardElementFor("bbq_energy"));
      }
      // ★ 2026-10-16：一键执行那一批的步骤**全部收尾**后，按钮要从「停止执行」
      // 变回「一键执行」。判据用"我们这批还有没有 pending/running 的步骤"，
      // 而不是"队列里还有没有东西" —— 队列里可能排着别人点的批次。
      // （必须在 setRunning 之前清：它内部会按这个标志决定按钮长什么样。）
      if (state.batchRunId) {
        const ours = (status.steps || []).filter(
          (step) => step.run_id === state.batchRunId
        );
        const unfinished = ours.some(
          (step) => step.status === "pending" || step.status === "running"
        );
        if (!unfinished) state.batchRunId = null;
      }
      // ★ 2026-10-16：把"哪些任务正在执行 / 还在排队"从服务端响应里**派生**出来。
      //
      // 服务端把"正在跑的那一批 + 排队批次的 pending 步骤"拼进 ``steps``
      // （webapi/state.py 的 status()），每项带 name / status / run_id。
      // 判据只用 pending / running ——
      // ⚠️ steps 里还混着 ``_recent`` 里**已完成**的最近 10 批，那条判据依赖
      // "已完成步骤的 status 只会是 ok/fail/skipped，永远不可能是 pending/running"
      // （tests/test_webapi_state_queue.py 里有一条测试钉住这一点）。
      const activeTasks = new Set();
      const activeRunIds = new Map();
      for (const step of status.steps || []) {
        if (step.status !== "pending" && step.status !== "running") continue;
        const name = step.name;
        if (!name) continue;
        activeTasks.add(name);
        // 同名步骤可能跨批次出现（每个主动任务后面都跟一个 daily_box）。
        // 取**第一条**：steps 的顺序就是它们会被执行的顺序，第一条 = 最先被处理的
        // 那一批，也正是「停止」该撤掉的对象。
        if (!activeRunIds.has(name)) activeRunIds.set(name, step.run_id || null);
      }
      state.activeTasks = activeTasks;
      state.activeRunIds = activeRunIds;
      // 乐观值的使命到此结束：这一份响应就是权威事实。留着它只会让"其实已经
      // 跑完"的按钮一直停在「停止执行」（也顺手覆盖了"提交后服务端没接住"的兜底）。
      state.optimisticRuns.clear();
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
          // 走 applyBanquetMeals：那份 meals 也带 expires_at，
          // 顺手把缓存有效期一起更新（漏了它 = 这份数据被当成永久有效）。
          applyBanquetMeals(bbqStep.data.meals);
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
        // ★ 2026-10-18：只读状态任务 top_pvp_status 也算"碰过它" ——
        // 它现在是 /api/player?full=1 用来刷新名次/积分的那一个。
        const touchedTopPvp = (status.steps || []).some(
          (step) => step.name === "top_pvp_battle" || step.name === "top_pvp_status"
        );
        renderAutoStatus();
        // ★ 2026-10-18：启动时"让行"欠下的那次统一读取在这里补上 ——
        // 用 full=1 一次拿全（含荣耀之巅），与原来 boot 那次等价。
        // 见 boot 里的 bootFullPlayerPending 说明。
        const needFull = state.bootFullPlayerPending;
        state.bootFullPlayerPending = false;
        // ★ 2026-10-18 晚：这三个 run_inline 抢**同一把**全局闸门，必须串起来。
        // 旧写法不 await，紧接着的 refreshMainStageStatus() 与
        // retryBanquetAfterRun() 会并发撞 409 —— "宴席提示每 5 秒一条"的直接来源
        // （快照本身要串行跑 5~6 个只读任务，20 秒级）。
        state.snapshotBusyUntil = Date.now() + 60000;
        void loadPlayer({ full: touchedTopPvp || needFull, quiet: true })
          .then((ok) => {
            // 队列里还有下一批时会被让行挡下（409）→ 把欠账还回去，
            // 等那批跑完再补，别把"启动那次统一读取"静默丢掉。
            if (needFull && !ok) state.bootFullPlayerPending = true;
          })
          .finally(() => refreshMainStageStatus())
          .finally(() => {
            state.snapshotBusyUntil = 0;
            // 闸门空了：允许立刻补读一次（不必再等退避）
            state.banquetRetryAt = 0;
            retryBanquetAfterRun();
          });
      }
      // ★ 运行期间被挡下的自动执行在这里补一次（见 runAutomation 的 pendingAuto）。
      // 【2026-10-04 修】旧条件（wasRunning 迁移）覆盖不了"提交 409 时本页还没见过
      // 任何运行"的场景 —— 典型就是刚登录：/api/player 只读还在跑、自动提交拿到
      // 409，而之后轮询永远见不到迁移，补跑就永远不会发生。改为"空闲且有欠账就补"。
      if (state.pendingAuto && !state.running) {
        state.pendingAuto = false;
        await runAutomation("补跑");
      }
      // ★ 2026-10-18：运行中没能读到的宴席时段，在这里补读一次。
      // 自动获取全程静默（不写日志）；快照在飞时会被 snapshotBusyUntil 挡住。
      retryBanquetAfterRun();
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
    // ★ 一键执行的成员顺序（2026-10-16）：服务端已排好序，前端照用 ——
    // 顺序规则（宴席第一、其余按规格表声明顺序）只在服务端实现一次。
    state.batchTasks = Array.isArray(payload.batch_tasks) ? payload.batch_tasks : [];
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
    // ★ 展开状态（2026-10-16）：与上面两段同款"服务端权威 + 本地兜底"。
    // updated_at 为空 = 服务端从未存过（升级后首启）→ 读 localStorage。
    // ⚠️ 必须排在 reconcileSelection / renderTaskGroups 之前：卡片的展开体
    // 是照着 state.expanded 造的，晚了就会先按"全折叠"渲染一版。
    const savedUiPrefs = payload.ui_prefs || {};
    if (savedUiPrefs.updated_at) {
      const names = Array.isArray(savedUiPrefs.expanded) ? savedUiPrefs.expanded : [];
      state.expanded = new Set(names.filter((name) => typeof name === "string"));
      try {
        localStorage.setItem(STORAGE_KEYS.uiPrefs, JSON.stringify([...state.expanded]));
      } catch {
        /* 存储被禁用时忽略 */
      }
    } else {
      state.expanded = readStoredExpanded();
    }
    reconcileSelection();
    // ★ 2026-10-19：**首帧只等 /api/tasks**（见 notes/task_list_first_paint_plan.md）。
    //
    // 【旧写法的问题】下面三个 ensure 都排在 renderTaskGroups() 之前，
    // 其中 ``ensureBanquetStatus()`` 是一次真实的 32007→32008 往返
    // （实测 ~2s，且宴席状态只在内存、每次开页面必发），活动候选在缓存过期时
    // 还要再串两次 run_inline（~3s）。结果就是"打开程序 → 两栏只剩一行读取中"。
    //
    // 【现在的口径】清单结构不依赖这三个请求（全在 /api/tasks 里），
    // 先画出来；候选 / 宴席随后补，补完只重画真正受影响的卡片。
    // ⚠️ 三个 ensure 仍然**保持顺序 await** —— 它们都走运行闸门（run_inline），
    // 并行发只会互相 409；而且 boot() 紧接着要提交自动任务，读必须先于写完成。
    state.hydrating = true;
    renderTaskGroups();
    try {
      for (const hydrate of [ensureActivityCache, ensureMaterialTree, ensureBanquetStatus]) {
        try {
          await hydrate();
        } catch {
          // 三个 ensure 各自都有失败兜底；这里只保证一个抛错不会带倒后面的补水。
        }
      }
    } finally {
      state.hydrating = false;
      refreshHydratedCards();
    }
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

    // --- 主动任务：一键执行工具行（★ 2026-10-16） ---
    // 同一个按钮两种身份：空闲时提交一批，运行中变「停止执行」。
    // 用 state.batchRunId 判身份而不是只看 state.running —— 否则"别的批次
    // 在跑、我没提交过任何东西"时点它会走进停止逻辑，白弹一句"没有可中断的运行"。
    const batchBtn = $("btn-run-manual");
    if (batchBtn) {
      batchBtn.addEventListener("click", () => {
        if (state.batchRunId && state.running) stopManualBatch();
        else runManualBatch();
      });
    }
    const selectAllBtn = $("btn-select-all");
    if (selectAllBtn) {
      selectAllBtn.addEventListener("click", () => setAllBatchPicks(true));
    }
    const selectNoneBtn = $("btn-select-none");
    if (selectNoneBtn) {
      selectNoneBtn.addEventListener("click", () => setAllBatchPicks(false));
    }
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
