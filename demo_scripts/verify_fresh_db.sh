#!/bin/bash
# 本地地址不走代理：VM 的 /etc/environment 设了 http_proxy 但没设 no_proxy，
# 连 127.0.0.1 都会被代理劫持 → curl 返回 502/空 → 健康检查假失败。
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
# verify_fresh_db.sh —— 验证「新建库 = 现有库」这条承诺真的成立
#
# 现在走 **alembic** 这条规范路径（而不是直接套 SQL 文件），
# 因为 alembic 才是以后建库的唯一入口。
#
# 步骤:
#   1. 建临时库
#   2. 检查「基线是否与 docs/database-schema.sql 一致」（防手工同步漂移）
#   3. alembic upgrade head（从零建全库）
#   4. 对比每个 schema 的表数 vs 现有库
#   5. 校验每个分区表都有「覆盖 now()」的分区
#   6. 分区表写入路由冒烟（插一行再删掉）
#   7. 清理临时库
#
# 用法: bash demo_scripts/verify_fresh_db.sh
set -u
DB=intensure_dev
VERIFY=schema_verify
VM="colima ssh -- bash -c"
FAIL=0

cd "$(dirname "$0")/.."

echo "=============================================="
echo " 新建库一致性验证"
echo "=============================================="

# ---------- 1. 建临时库 ----------
echo ""
echo "① 建临时库 $VERIFY"
colima ssh -- bash -c "sudo -u postgres psql -q -c 'DROP DATABASE IF EXISTS $VERIFY;' -c 'CREATE DATABASE $VERIFY;'" 2>&1 | grep -v NOTICE | sed 's/^/   /'

# ---------- 2. 基线 vs 规范文件 ----------
# 关键: 以后改 schema 的流程是「改 docs/database-schema.sql + 重新生成基线」。
# 如果只改了规范文件没重新生成, 基线就漂了 —— 这里直接 diff 出来。
echo ""
echo "② 检查基线与 docs/database-schema.sql 是否一致"
GEN_OUT=$(python3 db_maintenance/gen_baseline.py 2>&1)
if [ $? -ne 0 ]; then
    echo "   ❌ 生成器执行失败:"
    echo "$GEN_OUT" | sed 's/^/      /'
    FAIL=1
else
    # 生成器会就地改写 0001_baseline.py，所以用 git diff 判断是否漂移
    if git diff --quiet -- migrations/versions/0001_baseline.py 2>/dev/null; then
        echo "   ✅ 基线与规范文件一致"
    else
        echo "   ❌ 基线与规范文件**不一致** —— docs/database-schema.sql 改了但没重新生成基线"
        echo "      修复: python3 db_maintenance/gen_baseline.py && git add migrations/versions/"
        git diff --stat -- migrations/versions/0001_baseline.py | sed 's/^/      /'
        FAIL=1
    fi
fi

# ---------- 3. alembic upgrade head ----------
echo ""
echo "③ alembic upgrade head（从零建全库）"
OUT=$(colima ssh -- bash -c "cd /home/sunxiaoxuan.guest/intensure && ALEMBIC_DB_URL='postgresql:///$VERIFY' python3 -m alembic upgrade head" 2>&1)
RC=$?
echo "$OUT" | grep -E 'Running upgrade|ERROR' | sed 's/^/   /'
if [ "$RC" != "0" ]; then
    echo "   ❌ alembic upgrade 失败"
    echo "$OUT" | tail -8 | sed 's/^/      /'
    colima ssh -- bash -c "sudo -u postgres psql -q -c 'DROP DATABASE IF EXISTS $VERIFY;'" >/dev/null 2>&1
    exit 1
fi

# ---------- 4. 表数对比 ----------
echo ""
echo "④ 表数对比（新建库 vs 现有库）"
for q in assurance intensure shared translation implementation; do
    N=$(colima ssh -- bash -c "sudo -u postgres psql -d $VERIFY -t -A -c \"SELECT count(*) FROM information_schema.tables WHERE table_schema='$q';\"" 2>/dev/null | tr -d ' ')
    O=$(colima ssh -- bash -c "sudo -u postgres psql -d $DB -t -A -c \"SELECT count(*) FROM information_schema.tables WHERE table_schema='$q';\"" 2>/dev/null | tr -d ' ')
    if [ "$N" = "$O" ]; then
        echo "   ✅ $q: $N"
    else
        echo "   ❌ $q: 新建 $N / 现库 $O"
        FAIL=1
    fi
done

# retention 函数也要有
FN=$(colima ssh -- bash -c "sudo -u postgres psql -d $VERIFY -t -A -c \"SELECT count(*) FROM pg_proc WHERE proname='drop_old_partitions';\"" 2>/dev/null | tr -d ' ')
if [ "$FN" = "1" ]; then
    echo "   ✅ drop_old_partitions() 函数已建"
else
    echo "   ❌ drop_old_partitions() 函数缺失"
    FAIL=1
fi

# ---------- 5. 分区覆盖 ----------
echo ""
echo "⑤ 关键校验: 每个分区表都要有「覆盖 now()」的分区"
# ⚠️ 判定必须用「区间包含 now()」，不能只匹配 TO=当月 ——
#    覆盖当月的分区，它的 TO 是**下月**（10 月分区: FROM 2026-10-01 TO 2026-11-01）。
cat > /tmp/_vf_local.sql << 'SQLEOF'
WITH parents(inhparent) AS (
    SELECT DISTINCT inhparent FROM pg_inherits
), bounds AS (
    SELECT i.inhparent,
           (regexp_matches(pg_get_expr(c.relpartbound, c.oid),
               'FROM \(''([0-9]{4}-[0-9]{2}-[0-9]{2})'))[1]::date AS d_from,
           (regexp_matches(pg_get_expr(c.relpartbound, c.oid),
               'TO \(''([0-9]{4}-[0-9]{2}-[0-9]{2})'))[1]::date AS d_to
    FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
)
SELECT count(*) FROM (
    SELECT p.inhparent FROM parents p
    WHERE EXISTS (SELECT 1 FROM bounds b
                  WHERE b.inhparent = p.inhparent
                    AND b.d_from <= date_trunc('month', now())::date
                    AND b.d_to   >  date_trunc('month', now())::date)
) x;
SQLEOF
COVER=$(colima ssh -- bash -c "cat > /tmp/_vf.sql && sudo -u postgres psql -t -A -d $VERIFY -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>/dev/null | tr -d ' ')
TOTAL=7
if [ "$COVER" = "$TOTAL" ]; then
    echo "   ✅ $COVER/$TOTAL 个分区表都有覆盖当月的分区"
else
    echo "   ❌ 只有 $COVER/$TOTAL —— 有分区表缺当月分区，写入会报 no partition found"
    FAIL=1
fi

# ---------- 6. 写入路由冒烟 ----------
echo ""
echo "⑥ 写入冒烟测试（每个分区表插一行再删掉）"
# ⚠️ 用 DEFAULT VALUES 插入必然撞 NOT NULL，报的是「null 值违规」而非「分区缺失」——
#    而「null 值违规」恰恰**证明分区是找到了的**（行已成功路由到某个分区）。
#    所以只有 SQLERRM 含 'no partition of relation' 才算真失败。
cat > /tmp/_vf_local.sql << 'SQLEOF'
DO $$
DECLARE t text; routed int := 0; nopart int := 0;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'intensure.state_history','intensure.probe_results','intensure.port_events',
    'intensure.link_events','assurance.consistency_checks','assurance.healing_history',
    'shared.audit_log'] LOOP
    BEGIN
      EXECUTE format('INSERT INTO %s DEFAULT VALUES', t);
      EXECUTE format('DELETE FROM %s WHERE ctid IN (SELECT ctid FROM %s LIMIT 1)', t, t);
      routed := routed + 1;
    EXCEPTION WHEN others THEN
      IF SQLERRM LIKE '%no partition of relation%' THEN
        RAISE NOTICE '分区缺失 % : %', t, SQLERRM;
        nopart := nopart + 1;
      ELSE
        routed := routed + 1;
      END IF;
    END;
  END LOOP;
  RAISE NOTICE '路由成功 % 个, 分区缺失 % 个', routed, nopart;
END $$;
SQLEOF
OUT=$(colima ssh -- bash -c "cat > /tmp/_vf.sql && sudo -u postgres psql -d $VERIFY -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>&1)
NOPART=$(echo "$OUT" | grep -oE '分区缺失 [0-9]+ 个' | grep -oE '[0-9]+' | head -1)
ROUTED=$(echo "$OUT" | grep -oE '路由成功 [0-9]+ 个' | grep -oE '[0-9]+' | head -1)
NOPART=${NOPART:-0}
ROUTED=${ROUTED:-0}
echo "   路由成功 $ROUTED 个, 分区缺失 $NOPART 个"
if [ "$NOPART" -eq 0 ] && [ "$ROUTED" -eq 7 ]; then
    echo "   ✅ 7/7 分区表写入路由正常（无 no partition found）"
else
    echo "   ❌ 有分区表写入路由失败"
    echo "$OUT" | grep '分区缺失' | head -5 | sed 's/^/      /'
    FAIL=1
fi

# ---------- 7. 清理 ----------
echo ""
echo "⑦ 清理临时库"
colima ssh -- bash -c "sudo -u postgres psql -q -c 'DROP DATABASE IF EXISTS $VERIFY;'" 2>&1 | grep -v NOTICE | sed 's/^/   /'

echo ""
echo "=============================================="
if [ "$FAIL" = "0" ]; then
  echo " ✅ 全部通过 —— 新建库 = 现有库"
else
  echo " ❌ 有问题，见上面 ❌ 行"
fi
echo "=============================================="
exit $FAIL
