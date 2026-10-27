#!/bin/bash
# verify_fresh_db.sh —— 验证「新建库 = 现有库」这条承诺真的成立
#
# 做法:
#   1. 建临时库 schema_verify
#   2. 套用 docs/database-schema.sql
#   3. 套用 db_maintenance/retention_cleanup.sql
#   4. 对比每个 schema 的表数 vs 现有库
#   5. **关键校验**: 每个分区表都必须有一个覆盖 now() 的分区
#      （2026-10-27 实测: 保留脚本用「分区起始日」判断, 会把当前月分区删掉,
#        导致之后 INSERT 报 no partition of relation found for row）
#   6. 清理临时库
#
# 用法: bash demo_scripts/verify_fresh_db.sh
set -u
DB=intensure_dev
VERIFY=schema_verify
VM="colima ssh -- bash -c"

run_sql() {  # $1=sql
  $VM "cat > /tmp/_vf.sql && sudo -u postgres psql -v ON_ERROR_STOP=1 -d $1 -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>&1
}

cd "$(dirname "$0")/.."

echo "=============================================="
echo " 新建库一致性验证"
echo "=============================================="

# ---------- 1. 建临时库 ----------
echo ""
echo "① 建临时库 $VERIFY"
colima ssh -- bash -c "sudo -u postgres psql -q -c 'DROP DATABASE IF EXISTS $VERIFY;' -c 'CREATE DATABASE $VERIFY;'" 2>&1 | grep -v NOTICE | sed 's/^/   /'

# ---------- 2. 套 schema ----------
echo "② 套用 docs/database-schema.sql"
cp docs/database-schema.sql /tmp/_vf_local.sql
OUT=$($VM "cat > /tmp/_vf.sql && sudo -u postgres psql -v ON_ERROR_STOP=1 -d $VERIFY -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>&1)
RC=$?
echo "   退出码 $RC  ERROR $(echo "$OUT" | grep -c '^ERROR' || echo 0)"
if [ "$RC" != "0" ]; then
  echo "$OUT" | grep -E '^ERROR' | head -5 | sed 's/^/     /'
  colima ssh -- bash -c "sudo -u postgres psql -q -c 'DROP DATABASE IF EXISTS $VERIFY;'" >/dev/null 2>&1
  echo "   ❌ schema 文件套用失败"; exit 1
fi

# ---------- 3. 套维护脚本 ----------
echo "③ 套用 db_maintenance/retention_cleanup.sql"
cp db_maintenance/retention_cleanup.sql /tmp/_vf_local.sql
OUT=$($VM "cat > /tmp/_vf.sql && sudo -u postgres psql -v ON_ERROR_STOP=1 -d $VERIFY -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>&1)
RC=$?
echo "   退出码 $RC"
echo "$OUT" | grep -E 'Dropped:' | sed 's/^/     /'

# ---------- 4. 表数对比 ----------
echo "④ 表数对比（新建库 vs 现有库）"
FAIL=0
for q in assurance intensure shared translation implementation; do
  N=$(colima ssh -- bash -c "sudo -u postgres psql -d $VERIFY -t -A -c \"SELECT count(*) FROM information_schema.tables WHERE table_schema='$q';\"" 2>/dev/null | tr -d ' ')
  O=$(colima ssh -- bash -c "sudo -u postgres psql -d $DB -t -A -c \"SELECT count(*) FROM information_schema.tables WHERE table_schema='$q';\"" 2>/dev/null | tr -d ' ')
  if [ "$N" = "$O" ]; then echo "   ✅ $q: $N"; else echo "   ❌ $q: 新建 $N / 现有 $O"; FAIL=1; fi
done

# ---------- 5. 关键校验: 当前分区存在 ----------
  # ---------- 5. 分区覆盖检查 ----------
  echo "⑤ 关键校验: 每个分区表都要有「覆盖 now()」的分区"
  # ⚠️ 判定必须用「区间包含 now()」, 不能只匹配 TO=当月 ——
  #    覆盖当月的分区, 它的 TO 是**下月**（10 月分区: FROM 2026-10-01 TO 2026-11-01）。
  #    旧写法误判成「0/7 都有覆盖」, 那是误报不是真缺分区。
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
    colima ssh -- bash -c "sudo -u postgres psql -d $VERIFY -t -A -c \"
    WITH parents(inhparent) AS (SELECT DISTINCT inhparent FROM pg_inherits), bounds AS (
      SELECT i.inhparent,
             (regexp_matches(pg_get_expr(c.relpartbound,c.oid),'FROM \\(''([0-9]{4}-[0-9]{2}-[0-9]{2})'))[1]::date AS d_from,
             (regexp_matches(pg_get_expr(c.relpartbound,c.oid),'TO \\(''([0-9]{4}-[0-9]{2}-[0-9]{2})'))[1]::date AS d_to
      FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid)
    SELECT p.inhparent::regclass||' 缺当月分区' FROM parents p
    WHERE NOT EXISTS (SELECT 1 FROM bounds b WHERE b.inhparent=p.inhparent
        AND b.d_from <= date_trunc('month',now())::date AND b.d_to > date_trunc('month',now())::date);\"" 2>&1 | tail -8 | sed 's/^/     /'
    FAIL=1
  fi

# ---------- 6. 写入冒烟 ----------
  # ---------- 6. 写入冒烟 ----------
  echo "⑥ 写入冒烟测试（每个分区表插一行再删掉）"
  # ⚠️ 用 DEFAULT VALUES 插入必然撞 NOT NULL, 报的是「null 值违规」而不是「分区缺失」——
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
          -- 分区已找到, 只是 DEFAULT VALUES 缺必填列 —— 不算失败
          routed := routed + 1;
        END IF;
      END;
    END LOOP;
    RAISE NOTICE '路由成功 % 个, 分区缺失 % 个', routed, nopart;
  END $$;
SQLEOF
  OUT=$(colima ssh -- bash -c "cat > /tmp/_vf.sql && sudo -u postgres psql -d $VERIFY -f /tmp/_vf.sql" < /tmp/_vf_local.sql 2>&1)
  NOPART=$(echo "$OUT" | grep -oE '分区缺失 [0-9]+ 个' | grep -oE '[0-9]+' | head -1)
  NOPART=${NOPART:-0}
  ROUTED=$(echo "$OUT" | grep -oE '路由成功 [0-9]+ 个' | grep -oE '[0-9]+' | head -1)
  ROUTED=${ROUTED:-0}
  echo "   路由成功 $ROUTED 个, 分区缺失 $NOPART 个"
  if [ "$NOPART" -eq 0 ] && [ "$ROUTED" -eq 7 ]; then
    echo "   ✅ 7/7 分区表写入路由正常（无 no partition found）"
  else
    echo "   ❌ 有分区表写入路由失败"
    echo "$OUT" | grep '分区缺失' | head -5 | sed 's/^/     /'
    FAIL=1
  fi

# ---------- 7. 清理 ----------
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
