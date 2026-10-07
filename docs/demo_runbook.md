# 演示 Runbook（照本宣科版）

> **时间**：汇报当天
> **场景**：老师/同学看着你演示
> **核心命令**：一条 `bash demo_db_pipeline.sh`

---

## 🚀 开场前 5 分钟准备

```bash
# 1. 确保 Colima VM 在跑
colima status

# 2. SSH 进 VM
colima ssh

# 3. 进入项目目录
cd /home/sunxiaoxuan.guest/intensure
```

---

## 🎬 演示流程（**实时操作**）

### 步骤 0：展示文档（30 秒）

打开 `docs/presentation.md`（或打印讲稿）让老师扫一眼大纲：

> "我做了 **4 个核心方法**、**5 个功能模块**、**1 个一键演示脚本**。先看脚本能跑通什么。"

### 步骤 1：跑一键演示（90 秒）

**运行命令**：

```bash
DEMO_AUTO=1 bash demo_scripts/demo_db_pipeline.sh
```

> `DEMO_AUTO=1` 让"按回车继续"自动跳过，省时间

**解说节奏**：

| 步骤 | 何时说 |
|---|---|
| 1/8 启动 os-ken 控制器 | "控制器起在 tmux 里——这是我们的状态采集核心" |
| 2/8 启动 Mininet | "4 主机 1 交换机——真实 SDN 拓扑" |
| | "触发 ping，**主机学到 IP**"（强调：IP 是 L3 协议学到的，不是配置的）|
| 3/8 seed 策略 | "mock 实现模块写 2 条 ACCESS_CONTROL 策略" |
| 4/8 启动保障模块 | "**这是核心**——每 5s 跑一致性检查" |
| 5/8 健康基线 | "等 8 秒，看现在状态" |
| | "**2 pass, 0 violated** ✅ 网络健康" |
| 6/8 注入故障 | "**重点**——我注入故障" |
| | "10 秒后看结果——**2 violated + 2 diagnoses**" |
| | **"看 confidence=0.85**——这是因为我们把真实端口状态填进了 down_ports"** |
| 7/8 恢复 | "链路恢复——回 pass ✅" |
| 8/8 统计 | "158 条 consistency_checks + 120 条 diagnoses" |

### 步骤 2：现场查询（30 秒）

演示完脚本，立刻打开另一个终端：

```bash
colima ssh
sudo -u postgres psql -d intensure_dev
```

**关键查询 1**：越权测试

```sql
SET ROLE translation_user;
DELETE FROM implementation.policies WHERE intent_id = 'test';
-- ERROR: permission denied for table policies  ✅

SET ROLE implementation_user;
INSERT INTO translation.parsed_intents (idempotency_key, raw_nl, parsed_json)
  VALUES ('test', 'test', '{}'::jsonb);
-- ERROR: permission denied for table parsed_intents  ✅

RESET ROLE;
```

**关键查询 2**：诊断详情

```sql
SELECT intent_id, confidence, affected_devices,
       root_causes->0->>'type' AS cause,
       evidence->'down_ports' AS down_evidence
FROM assurance.diagnoses
ORDER BY diagnosed_at DESC LIMIT 3;
```

**关键查询 3**：表清单

```sql
SELECT table_schema, count(*)
FROM information_schema.tables
WHERE table_schema IN ('intensure', 'translation', 'implementation', 'assurance', 'shared')
GROUP BY table_schema ORDER BY 1;
```

---

## 🛑 应急情况

### 如果一键脚本卡住

```bash
# 检查哪个步骤卡了
tmux ls                # 看 sessions
tail -30 /tmp/mn.log    # mn 日志
tail -30 /tmp/assurance.log  # 保障模块日志
tail -30 /tmp/osken.log # 控制器日志
```

### 如果演示环境崩了（5 分钟内恢复）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure

# 1. 杀干净
tmux kill-session -t intensure 2>/dev/null
tmux kill-session -t mininet 2>/dev/null
sudo pkill -9 mn 2>/dev/null
sudo mn -c 2>/dev/null

# 2. 重跑
DEMO_AUTO=1 bash demo_scripts/demo_db_pipeline.sh
```

### 如果只是 Mininet 起不来

**直接跳到查询** —— 跟老师讲：

> "Mininet 没起来不影响核心演示，我直接展示 DB 数据 —— **保障模块已经写了 100+ 行诊断**，证明闭环跑过。"

```sql
SELECT count(*), result FROM assurance.consistency_checks GROUP BY result;
SELECT count(*) FROM assurance.diagnoses;
SELECT * FROM shared.module_health;
```

### 如果你想现场手注入故障（高级）

```bash
# 拿到 h1 进程 ID
H1_PID=$(pgrep -f "mininet:h1" | head -1)

# 断 h1 接口
sudo mnexec -a $H1_PID ip link set h1-eth0 down

# 等 10s 看保障模块的诊断
sudo -u postgres psql -d intensure_dev -c \
  "SELECT * FROM assurance.diagnoses ORDER BY diagnosed_at DESC LIMIT 3;"

# 恢复
sudo mnexec -a $H1_PID ip link set h1-eth0 up
```

---

## 🧹 演示结束清理

```bash
tmux kill-session -t intensure 2>/dev/null
tmux kill-session -t mininet 2>/dev/null
sudo pkill -9 mn 2>/dev/null
sudo mn -c 2>/dev/null
```

---

## 📋 演示中可能要打开的文件

| 文件 | 时机 |
|---|---|
| `docs/presentation.md` | 开头展示大纲 |
| `docs/speaker_notes.md` | 自己看，提醒话术 |
| `docs/database-architecture.md` | 被问到设计原则时 |
| `docs/database-schema.sql` | 被问到表结构时 |
| `docs/access-control-matrix.md` | 越权测试演示时 |

---

## 🎯 汇报流程总览

```
1. (5min) 课前准备：colima up，SSH VM
2. (1min)  展示 presentation.md 大纲
3. (2min) 讲方法（4 条铁律 + 5 schema）
4. (2min) 现场演示越权测试（DB 双保险）
5. (90s)  一键演示 demo_db_pipeline.sh
6. (1min) 现场查询 DB 数据
7. (3min) 讲 09-19 关键因果
8. (2min) 总结 + 未完成
9. (4min) Q&A
─────────────────────────────────
总计：~17 分钟
```

---

## 📞 万能救场语句

| 场景 | 话术 |
|---|---|
| 演示出错 | "**演示环境偶尔抖动**，我直接看 DB 数据 —— 这是更可信的证据" |
| 老师质疑设计 | "**这就是 4 条铁律要解决的问题**——我特意做了两层防护" |
| 时间不够 | "**我重点演示核心闭环**：健康 → 故障 → 诊断 → 恢复" |
| 被问"为什么不…？" | "好问题，**我目前的取舍是 X**，Phase 2 会考虑 Y" |

---

_Runbook v0.1 · 写于 2026-10-08 · 演示前一天再读一遍_