#!/usr/bin/env bash
# ============================================================================
# 启动 DmfDataAgent 后端 API 服务 (FastAPI + uvicorn)
# 用法:
#   ./run.sh                          # 默认 0.0.0.0:8000
#   PORT=9000 ./run.sh                # 换端口
#   RELOAD=true ./run.sh              # 开发模式热重载
# 说明:
#   - 通过模块入口 `python -m dmf_agent` 启动（见 dmf_agent/__main__.py）
#   - 友好变量 HOST / PORT / RELOAD 会映射为 DMF_APP-* 供 pydantic-settings 读取
# 前置:
#   1) 依赖需已装入「本目录自己的 .venv」（uv 管理，见下方 install 提示）
#   2) 建议先启动 embedding-service (默认 http://localhost:8001)，
#      否则 /health 中 embedding 组件会显示 down（服务本身仍可启动）
#   3) MySQL / milvus_lite 由 .env 中的 DMF_DB-* / DMF_RAG-* 控制
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_VENV="$SCRIPT_DIR/.venv"

# ---- 可通过环境变量覆盖的启动参数 ----
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
RELOAD="${RELOAD:-true}"

if [ ! -x "$PROJECT_VENV/bin/python" ]; then
  echo "❌ 未找到虚拟环境: $PROJECT_VENV/bin/python"
  echo "   请先创建并安装依赖 (uv):"
  echo "     uv venv && source .venv/bin/activate"
  echo "     uv pip install -e .  # 或按需 extras: uv pip install -e '.[milvus,dev]'"
  exit 1
fi

# ---- 快速预检: 核心依赖可导入（提前暴露缺包/语法错误）----
if ! "$PROJECT_VENV/bin/python" -c "import fastapi, uvicorn, dmf_agent" >/dev/null 2>&1; then
  echo "❌ 依赖不完整: fastapi / uvicorn / dmf_agent 无法导入"
  echo "   请先安装: source .venv/bin/activate && uv pip install -e '.[milvus]'"
  exit 1
fi

echo "▶ 启动 DmfDataAgent 后端: host=$HOST port=$PORT reload=$RELOAD"
echo "  venv: $PROJECT_VENV"
echo "  入口: python -m dmf_agent"
echo "  接口: http://localhost:$PORT/api/v1/health"
echo "  提示: 建议先启动 embedding-service (./embedding-service/run.sh, 端口 8001)"

# ---- 友好变量 -> DMF_APP-* 配置（python -m dmf_agent 经 pydantic-settings 读取）----
# 注意：pydantic-settings 的嵌套 env 名形如 `DMF_APP-HOST`（含连字符，由
# env_prefix="DMF_" + env_nested_delimiter="-" 拼出），而 bash 变量名**不允许
# 连字符**，无法直接 `export DMF_APP-HOST=...`（会报 not a valid identifier 并
# 因 set -e 终止脚本）。改用 `env` 在启动 python 子进程时注入这些环境变量。
exec env "DMF_APP-HOST=$HOST" "DMF_APP-PORT=$PORT" "DMF_APP-DEBUG=$RELOAD" \
    "$PROJECT_VENV/bin/python" -m dmf_agent
