#!/usr/bin/env bash
# demo_scripts/lib/ui.sh —— 演示脚本共用的 UI 工具
#
# ⚠️ 这个文件**曾经从来没进过版本库**，而 demo_scripts/00~99 全部 source 它
#    → 整条联调演示脚本链（预检 → 注册 → 采集 → 基线 → 注故障 → 违例 →
#    诊断 → 自愈 → 验证 → 清理）**从来没能真正跑起来**，一跑就是一屏
#    "ui_title: command not found"。2026-11-06 补上。
#
# 提供：
#   ui_title / ui_section / ui_step / ui_ok / ui_err / ui_warn / ui_kv / ui_pause
#   status_badge  状态徽章（ACTIVE / VIOLATED / RECOVERED / ...）
#   json_get      从 stdin 的 JSON 里取顶层字段
#   json_pretty   把 stdin 的 JSON 美化输出
#
# 约定：
#   - 非 TTY（管道 / CI / 自动化）时自动去掉颜色、ui_pause 不等待
#   - 不设 set -e（是被 source 的，不能影响调用方）

# ---------- 颜色 ----------
# 两套变量名：脚本里既有 C_* 也有 R/G/Y/N/B/BOLD/DIM/BASE，两套都提供。
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''
fi
C="$C_CYAN"

# 简写别名（脚本里用的是这套）
R="$C_RED"; G="$C_GREEN"; Y="$C_YELLOW"; N="$C_RESET"
B="$C_CYAN"; BOLD="$C_BOLD"; DIM="$C_DIM"

export C C_RESET C_BOLD C_DIM C_GREEN C_YELLOW C_RED C_CYAN
export R G Y N B BOLD DIM

# ---------- 路径与默认地址 ----------
SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export SCRIPT_DIR PROJECT_DIR

# 对方保障模块地址。
#
# ⚠️ 默认值是**联调测试期的临时隧道地址**（cloudflared 起的临时域名，重启就换），
#    **不是最终部署要用的地址** —— 最终地址要等对方服务正式部署后另行提供。
#    2026-11-06 复测：这个默认地址已经是 HTTP 000（隧道本身失效，直连也是 000）。
#    → 联调前请务必 export ASSURANCE_BASE=<对方给的新地址>
#
# 优先级：$BASE > $ASSURANCE_BASE > 默认联调临时地址
# （地址是运行时配置，从测试地址切到正式地址不用改任何代码）
BASE_DEFAULT="https://westminster-accounts-permitted-pursuant.trycloudflare.com"
BASE="${BASE:-${ASSURANCE_BASE:-$BASE_DEFAULT}}"
export BASE

# 用的是默认（很可能已失效）的联调地址 → 脚本据此提醒，别让人以为是网络问题
if [ -z "${ASSURANCE_BASE:-}" ] && [ "$BASE" = "$BASE_DEFAULT" ]; then
    BASE_IS_DEFAULT=1
else
    BASE_IS_DEFAULT=0
fi
export BASE_IS_DEFAULT

# ---------- UI ----------
ui_title() {
    echo ""
    echo "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════════════════${C_RESET}"
    echo "${C_BOLD}${C_CYAN}  $*${C_RESET}"
    echo "${C_BOLD}${C_CYAN}════════════════════════════════════════════════════════════════${C_RESET}"
}

ui_section() {
    echo ""
    echo "${C_BOLD}${C_CYAN}── $* ─────────────────────────────────${C_RESET}"
}

ui_step() {
    echo "${C_DIM}  →${C_RESET} $*"
}

ui_ok() {
    echo "${C_GREEN}  ✅${C_RESET} $*"
}

ui_err() {
    echo "${C_RED}  ❌${C_RESET} $*"
}

ui_warn() {
    echo "${C_YELLOW}  ⚠️ ${C_RESET} $*"
}

ui_kv() {
    printf "  ${C_DIM}%-22s${C_RESET} %s\n" "$1" "$2"
}

ui_pause() {
    # 非交互（管道 / CI / 自动化）时直接跳过，否则会挂死
    if [[ -t 0 ]]; then
        read -r -p "  ${C_DIM}按回车继续...${C_RESET}" _ || true
    fi
}

# ui_check_base —— 联调脚本开跑前提醒：现在用的地址是不是那个已失效的默认值
ui_check_base() {
    if [[ "${BASE_IS_DEFAULT:-0}" == "1" ]]; then
        ui_warn "当前用的是**默认的联调临时隧道地址**（大概率已失效，2026-11-06 实测 HTTP 000）"
        ui_step "正式地址要等对方部署后给；联调前请先 export ASSURANCE_BASE=<对方给的新地址>"
    else
        ui_kv "保障模块地址" "$BASE"
    fi
}

# ---------- 状态徽章 ----------
status_badge() {
    local st="${1:-}"
    case "$st" in
        ACTIVE)     echo "${C_GREEN}ACTIVE${C_RESET}" ;;
        REGISTERED) echo "${C_CYAN}REGISTERED${C_RESET}" ;;
        VIOLATED)   echo "${C_RED}VIOLATED${C_RESET}" ;;
        RECOVERED)  echo "${C_GREEN}RECOVERED${C_RESET}" ;;
        DIAGNOSING) echo "${C_YELLOW}DIAGNOSING${C_RESET}" ;;
        HEALING)    echo "${C_YELLOW}HEALING${C_RESET}" ;;
        *)          echo "${C_DIM}${st:-unknown}${C_RESET}" ;;
    esac
}

# ---------- JSON 小工具 ----------
# json_get "key"  —— 从 stdin 读 JSON，取顶层 key 的值（不存在或解析失败 → 空）
# ⚠️ 布尔值输出成 JSON 形式（true / false），不是 Python 的 True/False ——
#    调用方普遍拿它跟 "true" 比字符串。
json_get() {
    local key="${1:-}"
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
key = sys.argv[1]
if not isinstance(data, dict):
    sys.exit(0)
val = data.get(key, "")
if isinstance(val, bool):
    print("true" if val else "false")
elif val is None:
    print("")
elif isinstance(val, (dict, list)):
    print(json.dumps(val, ensure_ascii=False))
else:
    print(val)
' "$key"
}

# json_pretty —— stdin 的 JSON 美化输出（坏 JSON 原样透传，不吞内容）
json_pretty() {
    python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
except Exception:
    sys.stdout.write(raw)
'
}
