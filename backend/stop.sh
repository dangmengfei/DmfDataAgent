#!/usr/bin/env bash
# ============================================================================
# 停止 DmfDataAgent 后端 API 服务 (FastAPI + uvicorn, 默认 0.0.0.0:8000)
# 用法:
#   ./stop.sh            # 停止默认 8000 端口
#   PORT=9000 ./stop.sh  # 停止指定端口（需与 run.sh 的 PORT 一致）
# 行为:
#   1) 优先按「监听端口」定位 PID（最精准，避免误杀其它进程）
#   2) 兜底按进程命令行特征 dmf_agent.api.app:app 匹配
#   3) 先 SIGTERM 优雅退出，10s 内未退出再 SIGKILL
#   4) 复查端口是否释放
# ============================================================================
set -euo pipefail

PORT="${PORT:-8000}"

echo "▶ 停止 DmfDataAgent 后端 (端口 $PORT)..."

# 1) 优先按监听端口定位（兼容 0.0.0.0 / 127.0.0.1 绑定）
PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"

# 2) 兜底：按 uvicorn 应用标识匹配（进程在但端口暂未监听的场景）
if [ -z "$PIDS" ]; then
  PIDS="$(pgrep -f "dmf_agent.api.app:app" 2>/dev/null || true)"
fi

if [ -z "$PIDS" ]; then
  echo "ℹ️  未发现运行中的后端进程 (端口 $PORT)。"
  exit 0
fi

echo "  命中 PID: $PIDS"

# 3) 优雅终止
kill -TERM $PIDS 2>/dev/null || true

# 4) 等待退出，超时则强杀
for i in $(seq 1 10); do
  sleep 1
  if ! kill -0 $PIDS 2>/dev/null; then
    break
  fi
  if [ "$i" -eq 10 ]; then
    echo "  ⚠️  进程 10s 内未退出，发送 SIGKILL..."
    kill -KILL $PIDS 2>/dev/null || true
  fi
done

# 5) 复查端口
sleep 1
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "❌ 端口 $PORT 仍未释放，请手动检查: lsof -iTCP:$PORT -sTCP:LISTEN"
  exit 1
else
  echo "✅ 后端已停止，端口 $PORT 已释放。"
fi
