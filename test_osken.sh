#!/bin/bash
set -e
cd /home/sunxiaoxuan.guest/intensure

echo "=== 1. Python 直接 import ==="
python3 -c 'import simple_switch_13; print("import ok")' 2>&1

echo ""
echo "=== 2. 启 osken-manager (后台) ==="
osken-manager --verbose simple_switch_13 > /tmp/osken.log 2>&1 &
OSKEN_PID=$!
sleep 6

echo "=== 3. 进程状态 ==="
ps -p $OSKEN_PID -o pid,cmd 2>&1 | head -2 || echo "进程已退"

echo ""
echo "=== 4. 监听端口 ==="
ss -tln 2>/dev/null | grep -E "6633|6653" || echo "未监听"

echo ""
echo "=== 5. 完整日志 ==="
cat /tmp/osken.log

kill $OSKEN_PID 2>/dev/null
echo ""
echo "=== Done ==="