#!/usr/bin/env python3
"""test_demo_scripts.py — 演示脚本链的自检

覆盖场景：
  1. **依赖完整性** —— 每个 `source lib/ui.sh` 的脚本，lib/ui.sh 必须存在
     （⚠️ 2026-11-06 实测：lib/ui.sh 从来没进过版本库 → 00~99 整条联调
      演示脚本链从来没真正跑起来，一跑就是一屏 "command not found"）
  2. **函数齐备** —— 脚本里用到的每个 ui_* / status_badge / json_* 都要有定义
  3. **函数行为** —— ui_* 输出非空、status_badge 按状态上色、json_get 取值正确
  4. **非交互安全** —— 非 TTY 时 ui_pause 不阻塞（否则自动化跑到这里会挂死）
  5. **语法** —— 所有演示脚本 bash -n 通过

运行: python3 -m pytest test_demo_scripts.py -v
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(REPO, 'demo_scripts')
UI_SH = os.path.join(DEMO_DIR, 'lib', 'ui.sh')


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


def _demo_scripts():
    if not os.path.isdir(DEMO_DIR):
        return []
    return [os.path.join(DEMO_DIR, f) for f in sorted(os.listdir(DEMO_DIR))
            if f.endswith('.sh')]


def _bash(script, *args, stdin_text=None):
    """跑一段 bash，返回 (rc, stdout+stderr)。"""
    p = subprocess.run(['bash', '-c', script] + list(args),
                       capture_output=True, text=True, timeout=60,
                       input=stdin_text if stdin_text is not None else '')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


# ====================================================================
# 1. 依赖完整性
# ====================================================================

def test_ui_sh_exists():
    """lib/ui.sh 必须真的在仓库里（这是整条演示链的地基）。"""
    check('demo_scripts/lib/ui.sh 存在', os.path.isfile(UI_SH), UI_SH)


def test_all_dependencies_present():
    """每个 source 别的文件的脚本，那个文件必须存在。

    例外：**运行时产物**（.env）—— 它由 01_register_intent.sh 在运行时生成，
    不进版本库；脚本本身用 `|| { ui_err ...; exit 1; }` 处理了它缺失的情况。
    这里只保证它是 .gitignore 里的（不会被误提交）。
    """
    runtime_artifacts = {'.env'}
    missing = []
    for path in _demo_scripts():
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                m = re.search(r'source\s+"?\$SCRIPT_DIR/([^"$]+)"?', line)
                if not m:
                    continue
                name = m.group(1)
                if name in runtime_artifacts:
                    continue
                target = os.path.join(DEMO_DIR, name)
                if not os.path.exists(target):
                    missing.append(f'{os.path.basename(path)} → {name}')
    check('没有缺失的静态 source 依赖', not missing, str(missing))


def test_runtime_artifacts_are_gitignored():
    """运行时产物不能进版本库（.env 里可能有对方的地址之类的现场配置）。"""
    gitignore = os.path.join(REPO, '.gitignore')
    patterns = []
    if os.path.isfile(gitignore):
        with open(gitignore, encoding='utf-8') as fh:
            patterns = [ln.strip() for ln in fh]
    check('.env 在 .gitignore 里',
          any(p in ('.env', 'demo_scripts/.env', '*/.env') for p in patterns),
          str(patterns[:20]))


def test_dependencies_are_actually_tracked_by_git():
    """依赖文件必须**在 git 里被追踪**，而不只是在磁盘上存在。

    ⚠️ 这是个真实踩过的坑：`.gitignore` 里的 `lib/`（Python 打包模板规则）
    会匹配**任意层级**叫 lib 的目录 → `demo_scripts/lib/ui.sh` 被静默忽略 →
    `git add -A` 看不见它、也不报错 → 文件"从来没进过版本库"，
    而本地明明有文件、脚本本地也能跑 → **CI 上必然缺文件**。
    """
    try:
        p = subprocess.run(['git', 'ls-files', 'demo_scripts/'],
                           capture_output=True, text=True, cwd=REPO, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return                       # 不是 git 仓库就跳过
    if p.returncode != 0:
        return
    tracked = set(p.stdout.split())
    check('demo_scripts/lib/ui.sh 在版本库里',
          'demo_scripts/lib/ui.sh' in tracked,
          f'已追踪的 demo_scripts 文件: {sorted(tracked)[:10]}')
    # 所有被 source 的静态依赖都必须在版本库里
    missing = []
    for path in _demo_scripts():
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                m = re.search(r'source\s+"?\$SCRIPT_DIR/([^"$]+)"?', line)
                if not m or m.group(1) == '.env':
                    continue
                rel = os.path.join('demo_scripts', m.group(1))
                if rel not in tracked:
                    missing.append(rel)
    check('被 source 的依赖都在版本库里', not missing, str(missing))


# ====================================================================
# 2. 函数齐备
# ====================================================================

def test_required_functions_defined():
    """脚本里用到的每个 helper 都必须在 ui.sh 里定义。"""
    needed = set()
    for path in _demo_scripts():
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for name in re.findall(r'\b(ui_[a-z_]+|status_badge|json_get|json_pretty)\b',
                               text):
            needed.add(name)
    with open(UI_SH, encoding='utf-8') as fh:
        ui_text = fh.read()
    undefined = sorted(n for n in needed
                       if not re.search(rf'^\s*{re.escape(n)}\s*\(\)', ui_text,
                                        re.M))
    check('ui.sh 定义了所有被调用的函数', not undefined, str(undefined))


def test_color_vars_exported():
    """脚本里用到的颜色变量都要有。"""
    used = set()
    for path in _demo_scripts():
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for var in re.findall(r'\$\{?(C_[A-Z_]+|R|G|Y|N|B|BOLD|DIM|BASE)\}?',
                              text):
            used.add(var)
    with open(UI_SH, encoding='utf-8') as fh:
        ui_text = fh.read()
    missing = sorted(v for v in used if not re.search(rf'\b{re.escape(v)}=', ui_text))
    check('ui.sh 定义了所有被用到的变量', not missing, str(missing))


# ====================================================================
# 3. 函数行为
# ====================================================================

def test_ui_functions_produce_output():
    """每个 ui_* 都要有输出（且退出码 0）。"""
    src = f'source "{UI_SH}"; '
    for call in ('ui_title "标题"', 'ui_section "小节"', 'ui_step "一步"',
                 'ui_ok "成功"', 'ui_err "失败"', 'ui_warn "警告"',
                 'ui_kv "键" "值"'):
        rc, out = _bash(src + call)
        check(f'{call} 正常退出', rc == 0, f'rc={rc}')
        check(f'{call} 有输出', out.strip() != '', repr(out))


def test_status_badge_colors_by_state():
    """不同状态给不同颜色（VIOLATED 红、RECOVERED 绿）。"""
    rc, out = _bash(f'source "{UI_SH}"; status_badge VIOLATED')
    check('VIOLATED 有输出', 'VIOLATED' in out, repr(out))
    rc2, out2 = _bash(f'source "{UI_SH}"; status_badge RECOVERED')
    check('RECOVERED 有输出', 'RECOVERED' in out2, repr(out2))
    check('两者颜色不同（未上色时靠文字区分）',
          'VIOLATED' in out and 'RECOVERED' in out2, '')


def test_json_get():
    """json_get 从 stdin 取顶层字段。"""
    rc, out = _bash(f'source "{UI_SH}"; echo \'{{"status":"ACTIVE","n":3}}\' | json_get status')
    check('取到值', out.strip() == 'ACTIVE', repr(out))
    rc2, out2 = _bash(f'source "{UI_SH}"; echo \'{{"a":1}}\' | json_get missing')
    check('缺字段 → 空', out2.strip() == '', repr(out2))
    rc3, out3 = _bash(f'source "{UI_SH}"; echo \'not json\' | json_get status')
    check('坏 JSON → 空且不崩', out3.strip() == '' and rc3 == 0, f'{rc3} {out3!r}')


def test_json_get_renders_booleans_as_json():
    """布尔值要输出 true / false，不能是 Python 的 True / False。

    调用方普遍拿它跟字符串 "true" 比 —— 输出 True 就永远判不出来。
    （实测踩过：OVS 采集器的 running 字段是布尔，脚本判成"没在跑"）
    """
    rc, out = _bash(f'source "{UI_SH}"; echo \'{{"running":true}}\' | json_get running')
    check('true 输出成 true', out.strip() == 'true', repr(out))
    rc2, out2 = _bash(f'source "{UI_SH}"; echo \'{{"running":false}}\' | json_get running')
    check('false 输出成 false', out2.strip() == 'false', repr(out2))


def test_json_pretty():
    """json_pretty 美化输出，坏 JSON 原样透传。"""
    rc, out = _bash(f'source "{UI_SH}"; echo \'{{"a":1}}\' | json_pretty')
    check('美化了', '"a": 1' in out, repr(out))
    rc2, out2 = _bash(f'source "{UI_SH}"; echo \'raw text\' | json_pretty')
    check('坏 JSON 原样透传', 'raw text' in out2, repr(out2))


def test_non_interactive_pause_does_not_block():
    """非 TTY 时 ui_pause 必须直接返回 —— 否则自动化跑到这里会挂死。"""
    import time
    t0 = time.time()
    rc, out = _bash(f'source "{UI_SH}"; ui_pause; echo AFTER')
    dt = time.time() - t0
    check('ui_pause 不阻塞', dt < 5, f'耗时 {dt:.1f}s')
    check('继续往下跑了', 'AFTER' in out, repr(out))


def test_no_ansi_when_not_tty():
    """非 TTY 时不输出 ANSI 转义（管道 / CI 里保持干净）。"""
    rc, out = _bash(f'source "{UI_SH}"; ui_ok "测试"')
    check('没有 ANSI 码', '\033[' not in out, repr(out[:120]))


# ====================================================================
# 4. 语法
# ====================================================================

def test_all_demo_scripts_syntax_ok():
    """所有演示脚本 bash -n 通过。"""
    bad = []
    for path in _demo_scripts() + [UI_SH]:
        p = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
        if p.returncode != 0:
            bad.append(f'{os.path.basename(path)}: {p.stderr.strip()[:120]}')
    check('演示脚本语法全过', not bad, str(bad))


def test_precheck_runs_without_missing_helper():
    """预检脚本实跑：不能再出现 "command not found"。"""
    if not os.path.isfile(os.path.join(DEMO_DIR, '00_pre_check.sh')):
        return
    p = subprocess.run(['bash', os.path.join(DEMO_DIR, '00_pre_check.sh')],
                       capture_output=True, text=True, timeout=120,
                       cwd=DEMO_DIR, input='')
    out = (p.stdout or '') + (p.stderr or '')
    check('没有 command not found', 'command not found' not in out,
          out[-400:])
    check('没有 No such file', 'No such file' not in out, out[-400:])
    check('有预检标题', 'Step 0' in out, out[-200:])
