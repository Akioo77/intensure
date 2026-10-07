"""Seed 假数据到 implementation.policies（Phase 1 MVP 用）

为保障模块提供"期望状态"。Phase 1 用 implementation_user 写入（正常路径）。

用法:
    python3 seed_policies.py                # seed 默认 2 条
    python3 seed_policies.py --clear        # 先清空所有 active policy
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_writer import DBWriter

DSN_IMPL = os.environ.get(
    'IMPLEMENTATION_DB_DSN',
    'postgresql://implementation_user:implementation_dev_pwd@127.0.0.1:5432/intensure_dev'
)


DEFAULT_POLICIES = [
    {
        'intent_id': 'live-AC-demo-001',
        'intent_type': 'ACCESS_CONTROL',
        'policy_spec': {
            'source': {'cidr': '10.0.0.1/32'},
            'destination': {'cidr': '10.0.0.4/32'},
            'protocol': 'tcp',
            'destination_port': 443,
            'action': 'ALLOW',
        },
        'deployed_by': 'deploy-live-AC-demo-001',
    },
    {
        'intent_id': 'live-AC-demo-002',
        'intent_type': 'ACCESS_CONTROL',
        'policy_spec': {
            'source': {'cidr': '10.0.0.2/32'},
            'destination': {'cidr': '10.0.0.3/32'},
            'protocol': 'tcp',
            'destination_port': 80,
            'action': 'ALLOW',
        },
        'deployed_by': 'deploy-live-AC-demo-002',
    },
]


def seed(clear_first=False):
    w = DBWriter(dsn=DSN_IMPL)
    if clear_first:
        print('🧹 清空 active policies...')
        deleted = w.query("DELETE FROM implementation.policies WHERE status='active' RETURNING intent_id")
        print(f'   删除 {len(deleted)} 条: {[d["intent_id"] for d in deleted]}')

    print('🌱 Seed policies...')
    for p in DEFAULT_POLICIES:
        # 检查是否已存在
        existing = w.query_one(
            "SELECT id FROM implementation.policies WHERE intent_id = %s AND status='active'",
            (p['intent_id'],),
        )
        if existing:
            print(f'   ⏩ {p["intent_id"]} 已存在，跳过')
            continue
        try:
            with w._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO implementation.policies
                            (intent_id, intent_type, policy_spec, deployed_by)
                        VALUES (%s, %s, %s::jsonb, %s)
                        RETURNING id
                    """, (
                        p['intent_id'],
                        p['intent_type'],
                        json.dumps(p['policy_spec']),
                        p['deployed_by'],
                    ))
                    pid = cur.fetchone()[0]
            print(f'   ✅ {p["intent_id"]} → {p["intent_type"]} (id={pid})')
        except Exception as e:
            print(f'   ❌ {p["intent_id"]} 失败: {e}')

    # 验证
    print()
    print('📋 当前 active policies:')
    rows = w.query("SELECT intent_id, intent_type, policy_spec->>\'destination\' AS dst FROM implementation.policies WHERE status='active'")
    for r in rows:
        print(f'   • {r}')


if __name__ == '__main__':
    import json as _json
    json = _json  # alias
    clear = '--clear' in sys.argv
    seed(clear_first=clear)