/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./web/**/*.{html,js}",
  ],
  // ⚠️⚠️ 动态拼接的类名必须列在这里，否则会被整段摇掉 ——
  //   `conn conn--${kind}`（app.js / login.js::setConn）里的 kind 是运行时变量，
  //   Tailwind 的 content 扫描只认字面量，看不到 conn--ok / conn--bad。
  //   【实锤】2026-10-06 改版前查证：旧构建产物里 `conn--ok` 与 `conn--bad`
  //   出现次数均为 0 —— 也就是说"已连接"胶囊的绿色、"失败"的红色**从来就没
  //   生效过**，一直显示的是 .conn 继承来的默认字色。这正是本仓库在
  //   `.log-line--{LEVEL}` / `.log-mark--{ok,warn,fail}` 上栽过的同一个跟头。
  //   往 @layer components 里加新类前，先问一句：这个类名在源文件里是字面量吗？
  safelist: [
    "conn--wait",
    "conn--ok",
    "conn--bad",
    "conn--busy",
  ],
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        ios: {
          bg: "#000000",
          card: "#1c1c1e",
          cardHover: "#252528",
          elevated: "#2c2c2e",
          elevatedHover: "#363639",
          separator: "rgba(255, 255, 255, 0.08)",
          separatorStrong: "rgba(255, 255, 255, 0.16)",
          blue: "#007aff",
          blueHover: "#0a84ff",
          green: "#34c759",
          orange: "#ff9500",
          red: "#ff3b30",
          purple: "#af52de",
          teal: "#5ac8fa",
          muted: "#8e8e93",
          subtext: "#aeaeb2",
        },
      },
      fontFamily: {
        // 【为什么 system-ui 打头】三端各自命中自家最简约的原生 UI 字体：
        //   macOS/iOS = SF Pro，Windows = Segoe UI Variable/Segoe UI，
        //   Android = Roboto —— 西文永远不落到中文字体的拉丁字形上
        //   （旧栈把 Microsoft YaHei 排在 Segoe 之前，Win 端西文一直是雅黑字形，
        //   显胖显旧，这是"字体丑"的主要来源）。
        // 【中文字体按平台接力】mac = PingFang SC；华为 = HarmonyOS Sans SC；
        //   小米 = MiSans；原生/像素 = Noto Sans CJK SC；Windows 兜底 = 雅黑。
        //   全部是系统自带，不引入任何 webfont（手机端常常没有外网）。
        sans: [
          "system-ui",
          "-apple-system",
          '"Segoe UI Variable Text"',
          '"Segoe UI"',
          "Roboto",
          '"PingFang SC"',
          '"HarmonyOS Sans SC"',
          "MiSans",
          '"Noto Sans CJK SC"',
          '"Source Han Sans SC"',
          '"Microsoft YaHei UI"',
          '"Microsoft YaHei"',
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          '"SF Mono"',
          '"Cascadia Code"',
          "Consolas",
          "Menlo",
          "monospace",
        ],
      },
      borderRadius: {
        "ios-sm": "8px",
        "ios-md": "12px",
        "ios-lg": "16px",
        "ios-xl": "20px",
        "ios-2xl": "24px",
      },
      boxShadow: {
        "ios-subtle": "0 1px 2px rgba(0, 0, 0, 0.2)",
        "ios-card": "0 2px 12px rgba(0, 0, 0, 0.3)",
        "ios-modal": "0 12px 36px rgba(0, 0, 0, 0.6)",
      },
    },
  },
  plugins: [],
};
