"""自愈引擎 - 状态机管理自愈意图的完整生命周期。

状态机:
  idle → detecting → generating → pushing → verifying → done
                                                        ↓ (失败)
                                                     failed
                                                        ↓ (超阈值)
                                                     escalated → idle (人工)

数据流:
  1. 周期运行 consistency_check (或被动接受 deviation)
  2. 有偏差 → generating (调 healing_intent_generator)
  3. pushing (推送自愈意图给上游，模拟)
  4. verifying (等待若干秒后重新检测状态)
  5. 状态恢复 → done；未恢复 → failed / escalated

持久化:
  - 当前 cycles: /tmp/healing_cycles.json (活跃)
  - 历史: /tmp/healing_history.json (已 done/escalated)

REST 端点 (由 state_server.py 暴露):
  GET  /api/healing/status
  GET  /api/healing/history
  POST /api/healing/intent        (接收上游推送)
  POST /api/healing/trigger       (手动触发自愈 - 测试用)
  POST /api/healing/ack           (上游确认收到推送)
"""
import json
import os
import threading
import time
import uuid
from collections import deque
from typing import Dict, List, Optional

import healing_intent_generator as gen

# ============================================================
# 常量
# ============================================================

PERSIST_CYCLES_FILE = '/tmp/healing_cycles.json'
PERSIST_HISTORY_FILE = '/tmp/healing_history.json'

# 状态机
STATE_IDLE = 'idle'
STATE_DETECTING = 'detecting'
STATE_GENERATING = 'generating'
STATE_PUSHING = 'pushing'
STATE_VERIFYING = 'verifying'
STATE_DONE = 'done'
STATE_FAILED = 'failed'
STATE_ESCALATED = 'escalated'

TERMINAL_STATES = {STATE_DONE, STATE_FAILED, STATE_ESCALATED}

# 阈值
MAX_PUSH_ATTEMPTS = 3
MAX_VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL_S = 3.0  # 验证间隔
MAX_CYCLES_HISTORY = 50   # 历史保留最近 N 条

# 升级阈值: 同一 intent 在 M 次自愈后升级
MAX_CYCLES_BEFORE_ESCALATION = 3

# ============================================================
# 引擎
# ============================================================

class HealingEngine:
    """单例式自愈引擎."""

    def __init__(self):
        self._lock = threading.RLock()
        self._cycles: Dict[str, Dict] = {}     # cycle_id -> cycle dict
        self._history: deque = deque(maxlen=MAX_CYCLES_HISTORY)
        self._fail_counts: Dict[str, int] = {} # intent_id -> fail count (for escalation)
        # 模拟"上游接收端" - 实际项目里这是同学 B 的接口
        self._upstream_buffer: List[Dict] = []  # 等待 ack 的自愈意图
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        for path, target in [(PERSIST_CYCLES_FILE, 'cycles'),
                             (PERSIST_HISTORY_FILE, 'history')]:
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                if target == 'cycles' and isinstance(data, dict):
                    self._cycles = data
                elif target == 'history' and isinstance(data, list):
                    self._history = deque(data, maxlen=MAX_CYCLES_HISTORY)
            except Exception as e:
                print(f'[healing] load {target} failed: {e}')

    def _save_cycles(self):
        try:
            with open(PERSIST_CYCLES_FILE, 'w') as f:
                json.dump(self._cycles, f, indent=2, default=str)
        except Exception as e:
            print(f'[healing] save cycles failed: {e}')

    def _save_history(self):
        try:
            with open(PERSIST_HISTORY_FILE, 'w') as f:
                json.dump(list(self._history), f, indent=2, default=str)
        except Exception as e:
            print(f'[healing] save history failed: {e}')

    # ---------- 入口 ----------
    def detect_and_heal(self, intent: Dict, deviations: List[Dict]) -> Optional[str]:
        """检测偏差并启动自愈流程.

        参数:
          intent: 触发的意图
          deviations: 一致性检验返回的偏差列表

        返回: cycle_id (如果有启动), 否则 None
        """
        if not deviations:
            return None

        # 只处理 critical 级别（warning 只记录不自动自愈）
        critical = [d for d in deviations
                    if d.get('severity') in ('critical', 'error')]
        if not critical:
            return None

        # 取第一个 critical deviation 生成自愈意图
        dev = critical[0]
        healing_intent = gen.generate(dev, intent)

        with self._lock:
            cycle = self._new_cycle(intent, dev, healing_intent)
            self._cycles[cycle['cycle_id']] = cycle
            self._save_cycles()
            self._advance(cycle)

        return cycle['cycle_id']

    def trigger_manual(self, intent: Dict, dev: Dict) -> str:
        """手动触发自愈（测试用）."""
        healing_intent = gen.generate(dev, intent)
        with self._lock:
            cycle = self._new_cycle(intent, dev, healing_intent)
            cycle['manual'] = True
            self._cycles[cycle['cycle_id']] = cycle
            self._save_cycles()
            self._advance(cycle)
        return cycle['cycle_id']

    def ack(self, cycle_id: str) -> Dict:
        """上游确认收到推送."""
        with self._lock:
            cycle = self._cycles.get(cycle_id)
            if not cycle:
                return {'status': 'not_found'}
            cycle['acked_at'] = time.time()
            cycle['push_attempts'] += 1
            # 从上游 buffer 移除
            self._upstream_buffer = [
                h for h in self._upstream_buffer
                if h.get('cycle_id') != cycle_id
            ]
            self._advance(cycle)
            return {'status': 'ok', 'cycle_id': cycle_id,
                    'state': cycle['current_state']}

    # ---------- 状态机 ----------
    def _new_cycle(self, intent: Dict, dev: Dict, healing_intent: Dict) -> Dict:
        cycle_id = f'heal-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}'
        return {
            'cycle_id': cycle_id,
            'started_at': time.time(),
            'completed_at': None,
            'current_state': STATE_DETECTING,
            'intent_id': intent.get('intent_id', '?'),
            'intent_type': intent.get('semantics', {}).get('type', '?'),
            'deviation': dev,
            'healing_intent': healing_intent,
            'push_attempts': 0,
            'verify_attempts': 0,
            'last_update': time.time(),
            'events': [{'state': STATE_DETECTING,
                        'time': time.time(),
                        'note': f'检测到偏差: {dev.get("message", "?")}'}],
            'escalation_reason': None,
            'manual': False,
        }

    def _advance(self, cycle: Dict):
        """推进一个 cycle 的状态机（根据当前状态决定下一步）."""
        state = cycle['current_state']

        if state == STATE_DETECTING:
            self._to_generating(cycle)
        elif state == STATE_GENERATING:
            self._to_pushing(cycle)
        elif state == STATE_PUSHING:
            self._to_verifying(cycle)
        # VERIFYING 由 Timer 驱动 _verify_check，不在 _advance 里手动推进
        # done/failed/escalated 是终态

    def _to_generating(self, cycle: Dict):
        cycle['events'].append({
            'state': STATE_GENERATING,
            'time': time.time(),
            'note': f'生成自愈意图: {cycle["healing_intent"]["action"]}',
        })
        cycle['current_state'] = STATE_GENERATING
        cycle['last_update'] = time.time()
        self._advance(cycle)  # 立即进入下一步

    def _to_pushing(self, cycle: Dict):
        hi = cycle['healing_intent']
        # 推送到上游（这里是模拟）
        push_record = {
            'cycle_id': cycle['cycle_id'],
            'pushed_at': time.time(),
            'healing_intent': hi,
        }
        self._upstream_buffer.append(push_record)

        cycle['events'].append({
            'state': STATE_PUSHING,
            'time': time.time(),
            'note': f'推送至上游: action={hi["action"]}',
        })
        cycle['pushed_at'] = push_record['pushed_at']
        cycle['current_state'] = STATE_PUSHING
        cycle['last_update'] = time.time()
        # 自动模拟 ack（实际项目等真实上游响应）
        self._auto_ack(cycle)

    def _auto_ack(self, cycle: Dict):
        """自动 ack（模拟上游响应）。

        实际项目中: 同学 B 的意图实现智能体调 POST /api/healing/ack 确认
        """
        # 模拟 100ms 处理延迟
        threading.Timer(0.1, self.ack, args=[cycle['cycle_id']]).start()

    def _to_verifying(self, cycle: Dict):
        cycle['events'].append({
            'state': STATE_VERIFYING,
            'time': time.time(),
            'note': f'开始验证 (第 {cycle["verify_attempts"] + 1} 次)',
        })
        cycle['current_state'] = STATE_VERIFYING
        cycle['last_update'] = time.time()
        cycle['verify_attempts'] += 1
        self._save_cycles()

        # 异步等待后验证
        threading.Timer(VERIFY_INTERVAL_S, self._verify_check,
                        args=[cycle['cycle_id']]).start()

    def _verify_check(self, cycle_id: str):
        """验证当前网络状态是否恢复."""
        with self._lock:
            cycle = self._cycles.get(cycle_id)
            if not cycle or cycle['current_state'] != STATE_VERIFYING:
                return

            # 实际应重新跑 consistency_check
            # 这里简化: 假设"上游已修复"（在 verify_attempts >= 1 时模拟恢复）
            # 真实实现: import consistency_check, 重跑该 intent
            recovered = self._check_recovery(cycle)

            if recovered:
                cycle['recovered_at'] = time.time()
                cycle['current_state'] = STATE_DONE
                cycle['events'].append({
                    'state': STATE_DONE,
                    'time': time.time(),
                    'note': '验证通过，状态恢复',
                })
                cycle['completed_at'] = time.time()
                cycle['last_update'] = time.time()
                # 重置失败计数
                self._fail_counts.pop(cycle['intent_id'], None)
                # 移入历史
                self._history.append(cycle)
                del self._cycles[cycle_id]
                self._save_cycles()
                self._save_history()
            else:
                cycle['events'].append({
                    'state': STATE_VERIFYING,
                    'time': time.time(),
                    'note': f'第 {cycle["verify_attempts"]} 次验证未通过',
                })
                cycle['last_update'] = time.time()
                if cycle['verify_attempts'] >= MAX_VERIFY_ATTEMPTS:
                    self._to_failed(cycle)
                else:
                    self._to_verifying(cycle)  # 再次验证

    def _check_recovery(self, cycle: Dict) -> bool:
        """检查状态是否恢复. 实际应调 consistency_check.

        简化策略: 验证次数 >= 2 认为恢复 (测试场景)
        真实项目: 重新跑 consistency_check 该 intent, 看是否仍 violated
        """
        # TODO: 真实接入 consistency_check
        # from consistency_check import check_intent
        # intent = intent_registry.get(cycle['intent_id'])
        # report = check_intent(intent)
        # return report['status'] == 'compliant'

        # 简化: 假定推送后 2 次验证（约 6s）后恢复
        return cycle['verify_attempts'] >= 2

    def _to_failed(self, cycle: Dict):
        cycle['events'].append({
            'state': STATE_FAILED,
            'time': time.time(),
            'note': f'自愈失败 ({cycle["verify_attempts"]} 次验证未通过)',
        })
        cycle['current_state'] = STATE_FAILED
        cycle['completed_at'] = time.time()
        cycle['last_update'] = time.time()

        # 累计失败次数，决定是否升级
        intent_id = cycle['intent_id']
        self._fail_counts[intent_id] = self._fail_counts.get(intent_id, 0) + 1
        if self._fail_counts[intent_id] >= MAX_CYCLES_BEFORE_ESCALATION:
            cycle['current_state'] = STATE_ESCALATED
            cycle['escalation_reason'] = (
                f'该意图在最近 {MAX_CYCLES_BEFORE_ESCALATION} '
                f'次自愈中均失败，需人工介入'
            )
            cycle['events'].append({
                'state': STATE_ESCALATED,
                'time': time.time(),
                'note': cycle['escalation_reason'],
            })

        self._history.append(cycle)
        del self._cycles[cycle['cycle_id']]
        self._save_cycles()
        self._save_history()

    # ---------- 查询 ----------
    def status(self) -> Dict:
        """当前状态汇总."""
        with self._lock:
            active = list(self._cycles.values())
            return {
                'active_cycles': len(active),
                'escalated_count': sum(1 for c in self._history
                                       if c.get('current_state') == STATE_ESCALATED),
                'done_count': sum(1 for c in self._history
                                  if c.get('current_state') == STATE_DONE),
                'failed_count': sum(1 for c in self._history
                                    if c.get('current_state') == STATE_FAILED),
                'history_count': len(self._history),
                'upstream_buffer': len(self._upstream_buffer),
                'cycles': active,
            }

    def history(self, limit: int = 20) -> List[Dict]:
        """历史 cycles（最近 N 条）."""
        with self._lock:
            return list(self._history)[-limit:][::-1]

    def get_cycle(self, cycle_id: str) -> Optional[Dict]:
        with self._lock:
            return self._cycles.get(cycle_id) or next(
                (c for c in self._history if c['cycle_id'] == cycle_id), None)

    def upstream_buffer_snapshot(self) -> List[Dict]:
        """上游待 ack 的自愈意图."""
        with self._lock:
            return list(self._upstream_buffer)


# 单例
_engine: Optional[HealingEngine] = None


def get_engine() -> HealingEngine:
    global _engine
    if _engine is None:
        _engine = HealingEngine()
    return _engine