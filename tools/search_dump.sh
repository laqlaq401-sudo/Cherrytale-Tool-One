#!/usr/bin/env bash
# =============================================================================
# search_dump.sh —— dump.cs 大文件检索助手
# -----------------------------------------------------------------------------
# 【为什么需要这个脚本】
#   dump.cs 有 34.8 MB、20951 个类型。直接打开会卡死编辑器，
#   而把它整个读进上下文去「找一段代码」既没必要，也极易看漏周边信息。
#   正确做法是「正则定位 + 只看前后 20~30 行」——本脚本把这条规则固化成命令，
#   避免你每次手动敲长命令时看漏或看错。
#
# 【性能参考（本机实测）】
#   全库扫描一次只要约 0.08 秒，因此可以放心反复检索。
#
# 【用法】
#   bash tools/search_dump.sh '<正则>' [-C 行数] [-m 命中数] [-i] [--namespaces]
#
# 【示例】
#   bash tools/search_dump.sh 'class .*Req // TypeDefIndex'
#   bash tools/search_dump.sh 'com\.auer\.game\.protobuf' -C 30 -m 5
#   bash tools/search_dump.sh 'ProtoMember' -C 2 -m 10
#   bash tools/search_dump.sh 'AppSecret|CalcSign|GetSign' -i --namespaces
#
# 【环境变量】
#   CHERRYTALE_DUMP_CS  指定 dump.cs 的路径（默认取项目内的相对路径）
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DUMP_FILE="${CHERRYTALE_DUMP_CS:-$PROJECT_ROOT/Cherrytale IL2CPP/dump.cs}"

PATTERN=""
CONTEXT=25      # 默认前后各看 25 行，符合「20~30 行上下文」的约定
MAX_HITS=20     # 默认最多显示 20 处命中，避免一次刷满屏幕
CASE_FLAG=""
MODE="context"

print_help() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# 解析参数
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_help
      exit 0
      ;;
    -i)
      CASE_FLAG="-i"
      shift
      ;;
    -C)
      CONTEXT="${2:-25}"
      shift 2
      ;;
    -m)
      MAX_HITS="${2:-20}"
      shift 2
      ;;
    --namespaces)
      MODE="namespaces"
      shift
      ;;
    *)
      if [[ -z "$PATTERN" ]]; then
        PATTERN="$1"
      else
        echo "✘ 多余的参数：$1（模式串只能给一个，记得整体加引号）" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

if [[ -z "$PATTERN" ]]; then
  echo "✘ 请提供要检索的正则表达式。例如：" >&2
  echo "    bash tools/search_dump.sh 'class .*Req' -C 30 -m 5" >&2
  exit 2
fi

if [[ ! -f "$DUMP_FILE" ]]; then
  echo "✘ 找不到 dump.cs：$DUMP_FILE" >&2
  echo "  可用环境变量指定位置：" >&2
  echo "    export CHERRYTALE_DUMP_CS='/path/to/dump.cs'" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 先统计总命中数，让你对「这个关键词有多泛滥」有个判断
# ---------------------------------------------------------------------------
if [[ -n "$CASE_FLAG" ]]; then
  TOTAL="$(grep -c -E -i -- "$PATTERN" "$DUMP_FILE" || true)"
else
  TOTAL="$(grep -c -E -- "$PATTERN" "$DUMP_FILE" || true)"
fi

echo "========================================================================"
echo "文件    : $DUMP_FILE"
echo "模式    : $PATTERN"
echo "总命中  : $TOTAL 行"
if [[ "$MODE" == "context" ]]; then
  echo "上下文  : 前后各 $CONTEXT 行，最多展示 $MAX_HITS 处"
fi
echo "========================================================================"

if [[ "$TOTAL" == "0" ]]; then
  echo "（没有命中。可以试试放宽关键词，或先用 --namespaces 模式看命名空间分布）"
  exit 1
fi

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
if [[ "$MODE" == "namespaces" ]]; then
  # awk 的字符串字面量里，`\` 本身是转义引导符。
  # 若把用户写的 `com\.auer` 原样交给 awk，它会警告「\. 被当作 . 处理」，
  # 于是正则从「字面点」退化成「任意字符」。这里先把每个 `\` 双写，
  # 让 awk 正确收到 `\.`，既消除警告又保证语义与 grep 一致。
  AWK_PATTERN="${PATTERN//\\/\\\\}"

  # 单遍扫描：记录最近一次出现的 `// Namespace:`，命中时输出它。
  # 这样能立刻看清「这个关键词主要集中在哪个命名空间」，
  # 是快速建立全局认知最有效的手段。
  #
  # 关于空命名空间：dump.cs 里无命名空间的类型会写成 `// Namespace: `（末尾一个空格），
  # 此时 substr 取到空串，需要替换成可读标签，否则直方图里会出现一列看不清的空白。
  awk -v pat="$AWK_PATTERN" -v ic="$CASE_FLAG" '
    BEGIN {
      ns = "<全局命名空间（无 namespace）>"
      if (ic != "") { pat = tolower(pat) }
    }
    /^\/\/ Namespace:/ {
      ns = substr($0, 15)
      if (ns == "") { ns = "<全局命名空间（无 namespace）>" }
    }
    {
      target = (ic != "") ? tolower($0) : $0
      if (target ~ pat) print ns
    }
  ' "$DUMP_FILE" | sort | uniq -c | sort -rn | head -20
  echo "------------------------------------------------------------------------"
  echo "（以上为命中行的命名空间分布 Top 20，按命中数降序）"
else
  grep -n -E $CASE_FLAG -B "$CONTEXT" -A "$CONTEXT" -m "$MAX_HITS" \
    -- "$PATTERN" "$DUMP_FILE"
  echo "------------------------------------------------------------------------"
  if [[ "$TOTAL" -gt "$MAX_HITS" ]]; then
    echo "（共 $TOTAL 行命中，此处只显示前 $MAX_HITS 处；"
    echo "  想多看几处请加 -m 50，想收窄范围请把正则写得更具体）"
  fi
fi
