#!/usr/bin/env python3
"""安全落地伙伴的 assurance_schema.sql。

核心安全设计（吸取两次教训）：
  1. 曾用 BSD sed 的 \\b 做重写，因 BSD sed 不支持单词边界，规则静默失效，
     脚本原样打到真库，误建 5 张表（已清理复原）。现在改用 Python 正则。
  2. 曾用 bash -c "... $1" 传 SQL，colima ssh 不会传位置参数，
     psql 收到空串，查询静默返回空结果（冲突清单假空）。现在改 stdin 管道。

三道闸：
  闸1  脚本含 DROP/DELETE/TRUNCATE/ALTER/GRANT ALL -> 拒绝执行
  闸2  重写后仍存在未重写的裸 assurance. 引用 -> 拒绝执行（上次就翻在这）
  闸3  apply 前先 dry-run；存在硬冲突（同名表）-> 拒绝落地

dry-run 一律在独立 schema + 独立事务里跑，结束必删，现有库零影响。

用法:
  python3 demo_scripts/apply_partner_schema.py <partner_schema.sql>
  python3 demo_scripts/apply_partner_schema.py <partner_schema.sql> --apply
"""
import re
import sys
import subprocess

STAGING = "assurance_partner_staging"
DB = "intensure_dev"

DANGEROUS = [
    (r"\bDROP\s+(TABLE|SCHEMA|DATABASE)\b", "DROP TABLE/SCHEMA/DATABASE"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bDELETE\s+FROM\b", "DELETE FROM"),
    (r"\bALTER\s+TABLE\b", "ALTER TABLE"),
    (r"\bGRANT\s+ALL\b", "GRANT ALL"),
]


def ssh_q(sql):
    """在 VM 里跑一条查询, 返回 (rc, stdout, stderr)。"""
    # 不能用 bash -c "... $1"：colima ssh 不会把 SQL 作为位置参数传进去，
    # psql 会收到空串，查询静默返回空结果。改用 stdin 管道。
    inner = f"cat > /tmp/_q.sql && sudo -u postgres psql -v ON_ERROR_STOP=1 -d {DB} -t -A -F '|' -f /tmp/_q.sql"
    p = subprocess.run(["colima", "ssh", "--", "bash", "-c", inner],
                       input=sql, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def ssh_script(script):
    """把整段 SQL 送进 VM 执行。"""
    inner = f"cat > /tmp/_partner_run.sql && sudo -u postgres psql -v ON_ERROR_STOP=1 -d {DB} -f /tmp/_partner_run.sql"
    p = subprocess.run(["colima", "ssh", "--", "bash", "-c", inner],
                       input=script, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def main():
    if len(sys.argv) < 2:
        print("用法: python3 apply_partner_schema.py <file.sql> [--apply]")
        return 1
    src = sys.argv[1]
    apply_mode = "--apply" in sys.argv
    try:
        raw = open(src, encoding="utf-8").read()
    except OSError as e:
        print(f"读不到文件: {e}")
        return 1

    print("=" * 64)
    print(f"源文件: {src}  ({raw.count(chr(10)) + 1} 行)")
    creates = len(re.findall(r"(?im)^\s*CREATE\s+TABLE", raw))
    print(f"建表语句: {creates} 个")
    print("=" * 64)

    # 闸 1: 危险语句
    hits = []
    for pat, name in DANGEROUS:
        for m in re.finditer(pat, raw, re.I):
            line = raw[:m.start()].count(chr(10)) + 1
            hits.append(f"  {name} (第 {line} 行)")
    if hits:
        print("[闸1] 拦截: 脚本含危险语句")
    for h in hits:
        print(h)
    if hits:
        print("  理由: schema 脚本不该删表/改表/动数据。请让伙伴删掉这些语句。")
        return 2
    print("[闸1] 通过: 无危险语句")

    # 重写: 注释行原样保留, 其余把 assurance. 换成 STAGING.
    out_lines = []
    for line in raw.split(chr(10)):
        if line.lstrip().startswith("--"):
            out_lines.append(line)
            continue
        line = re.sub(r"(?i)^(\s*)CREATE\s+SCHEMA\b[^;]*;",
                      r"\1-- [skipped] CREATE SCHEMA ...;", line)
        line = re.sub(r"(?<![\w.])assurance\.", STAGING + ".", line)
        out_lines.append(line)
    rewritten = chr(10).join(out_lines)

    # 闸 2: 重写校验 —— 上次翻车就翻在这
    leftover = []
    for i, line in enumerate(rewritten.split(chr(10)), 1):
        if line.lstrip().startswith("--"):
            continue
        if re.search(r"(?<![\w.])assurance\.", line):
            leftover.append(f"  第 {i} 行: {line.strip()[:100]}")
    if leftover:
        print("[闸2] 拦截: 重写后仍有未重写的 assurance. 引用")
    for l in leftover[:10]:
        print(l)
    if leftover:
        print("  这些语句会打到真库。已中止，未执行任何 SQL。")
        return 3
    print(f"[闸2] 通过: 所有 assurance. 引用已重定向到 {STAGING}")

    # 独立 schema + 独立事务执行
    script = (
        "BEGIN;\n"
        f"DROP SCHEMA IF EXISTS {STAGING} CASCADE;\n"
        f"CREATE SCHEMA IF NOT EXISTS {STAGING};\n"
        f"SET LOCAL search_path TO {STAGING}, public;\n"
        f"{rewritten}\n"
        "COMMIT;\n"
    )
    rc, out, err = ssh_script(script)
    if rc != 0:
        print("执行失败:")
        print((err or out)[-1500:])
        ssh_q(f"DROP SCHEMA IF EXISTS {STAGING} CASCADE;")
        print("  已清理暂存 schema")
        return 4
    print("[闸3] 通过: 暂存执行成功")
    print()

    # 冲突分析
    q = (
        "SELECT coalesce(nt.table_name, ot.table_name) AS name, "
        "CASE WHEN nt.table_name IS NOT NULL AND ot.table_name IS NOT NULL "
        "THEN 'HARD_CONFLICT' WHEN nt.table_name IS NOT NULL THEN 'NEW' "
        "ELSE 'EXISTS_ONLY' END AS status, "
        "coalesce((SELECT count(*) FROM information_schema.columns c "
        f"WHERE c.table_schema='{STAGING}' AND c.table_name=nt.table_name),0) AS their_cols, "
        "coalesce((SELECT count(*) FROM information_schema.columns c "
        "WHERE c.table_schema='assurance' AND c.table_name=ot.table_name),0) AS our_cols "
        "FROM information_schema.tables nt "
        "FULL JOIN information_schema.tables ot "
        "ON ot.table_name=nt.table_name AND ot.table_schema='assurance' "
        f"WHERE nt.table_schema='{STAGING}' "
        "AND (ot.table_name IS NULL OR ot.table_schema='assurance') "
        "ORDER BY 2, 1;"
    )
    rc, out, _ = ssh_q(q)
    if not out.strip():
        print("冲突分析查询返回空, 中止（避免误判为无冲突）")
        ssh_q(f"DROP SCHEMA IF EXISTS {STAGING} CASCADE;")
        return 7

    print("表名 | 状态 | 伙伴列数 | 我们列数")
    print("-" * 64)
    conflicts = []
    new_count = 0
    for row in out.split(chr(10)):
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) < 4:
            continue
        name, status, c1, c2 = parts[0], parts[1], parts[2], parts[3]
        mark = {"HARD_CONFLICT": "硬冲突", "NEW": "新表"}.get(status, status)
        print(f"  {name:<24} {mark:<10} {c1:<9} {c2}")
        if status == "HARD_CONFLICT":
            conflicts.append(name)
        elif status == "NEW":
            new_count += 1
    print("-" * 64)
    print()
    print(f"新建 {new_count} 张, 硬冲突 {len(conflicts)} 张")

    # 列级差异
    if conflicts:
        print()
        print("=== 硬冲突表的列差异 ===")
        for t in conflicts:
            q2 = (
                "SELECT c.table_schema, c.column_name, c.data_type "
                "FROM information_schema.columns c "
                f"WHERE c.table_name='{t}' "
                f"AND c.table_schema IN ('{STAGING}','assurance') "
                "ORDER BY c.table_schema, c.ordinal_position;"
            )
            rc2, o2, _ = ssh_q(q2)
            ours, theirs = [], []
            for r in o2.split(chr(10)):
                p = r.split("|")
                if len(p) < 3:
                    continue
                if p[0] == "assurance":
                    ours.append(p[1] + " " + p[2])
                else:
                    theirs.append(p[1] + " " + p[2])
            print(f"  > {t}")
            for l in ours:
                print(f"      [我们] {l}")
            for l in theirs:
                print(f"      [伙伴] {l}")
            our_names = [x.split()[0] for x in ours]
            their_names = [x.split()[0] for x in theirs]
            only_ours = [n for n in our_names if n not in their_names]
            only_theirs = [n for n in their_names if n not in our_names]
            if only_ours:
                print("      只有我们有: " + ", ".join(only_ours))
            if only_theirs:
                print("      只有他有: " + ", ".join(only_theirs))

    # 清理暂存
    ssh_q(f"DROP SCHEMA IF EXISTS {STAGING} CASCADE;")
    print()
    print("暂存 schema 已删除, 现有库零影响")

    if not apply_mode:
        print()
        print("这是 DRY RUN。确认无冲突后, 加 --apply 真落地。")
        return 0

    # APPLY
    if conflicts:
        print()
        print("[APPLY] 存在硬冲突, 已拒绝落地")
        print("  冲突表: " + ", ".join(conflicts))
        print("  请先与伙伴确认: 合并列 / 改名 / 还是他改用我们的结构。")
        return 5
    print()
    print("=== APPLY: 先备份 ===")
    subprocess.run(["colima", "ssh", "--", "bash", "-c",
                    f"sudo -u postgres pg_dump -d {DB} -f /tmp/pre_apply.sql && "
                    "sudo cp /tmp/pre_apply.sql /home/sunxiaoxuan.guest/"], check=False)
    print("  备份: /home/sunxiaoxuan.guest/pre_apply.sql")
    rc3, o3, e3 = ssh_script(rewritten)
    if rc3 != 0:
        print("落地失败:")
        print((e3 or o3)[-1500:])
        return 6
    print("落地成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
