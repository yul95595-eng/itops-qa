#!/usr/bin/env bash
# ============================================================
# start.sh —— IT 运维知识库助手 一键启动脚本
#
# 作用: 把"检查依赖 → 拉基础设施 → 灌数据 → 起服务 → 冒烟验证"
#       串成一条命令, 解决"新人 clone 下来不知道怎么跑"的问题。
#
# 用法:
#   ./start.sh              # 完整启动(含冒烟测试)
#   ./start.sh --skip-ingest   # 数据已灌过, 只起服务+冒烟
#   ./start.sh --no-smoke      # 只起服务, 不跑冒烟
#
# 环境要求: docker + docker compose + python(建议 conda 环境已装好依赖)
# ============================================================
set -e

# ---- 颜色输出 ----
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- 解析参数 ----
SKIP_INGEST=false
NO_SMOKE=false
for arg in "$@"; do
  case $arg in
    --skip-ingest) SKIP_INGEST=true ;;
    --no-smoke)    NO_SMOKE=true ;;
    *) error "未知参数: $arg"; exit 1 ;;
  esac
done

# ---- 切到项目根目录 ----
cd "$(dirname "$0")"

# ---- 1. 检查 python ----
if ! command -v python >/dev/null 2>&1; then
  error "未找到 python, 请先安装并激活 conda 环境(itOpsQA)"
  exit 1
fi
info "Python: $(python --version 2>&1)"

# ---- 2. 检查 .env(LLM 密钥 + 容器口令) ----
if [ ! -f .env ]; then
  warn ".env 不存在, 请先配置 DASHSCOPE_API_KEY(参见 .env.example)"
else
  # 口令统一从 .env 读取, 脚本与 compose 用同一份, 不写死明文
  MYSQL_ROOT_PASSWORD=$(grep -E '^MYSQL_ROOT_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
  REDIS_PASSWORD=$(grep -E '^REDIS_PASSWORD=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
  export MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD:-your_password}
  export REDIS_PASSWORD=${REDIS_PASSWORD:-your_password}
fi

# ---- 3. 拉起基础设施(Milvus + MySQL + Redis) ----
info "检查 Docker 基础设施..."
if ! command -v docker >/dev/null 2>&1; then
  error "未找到 docker, 请先安装 Docker Desktop"
  exit 1
fi

if ! docker compose ps >/dev/null 2>&1; then
  info "基础设施未运行, 正在拉起 (MySQL + Redis + Milvus)..."
  docker compose up -d
else
  info "基础设施已在运行"
fi

# 等待 MySQL 就绪
info "等待基础设施就绪..."
for i in $(seq 1 30); do
  if docker compose exec -T mysql mysqladmin ping -h localhost -p"${MYSQL_ROOT_PASSWORD:-your_password}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# ---- 4. 灌入内置数据(幂等, 可跳过) ----
if [ "$SKIP_INGEST" = false ]; then
  info "灌入内置数据(错误码 + FAQ + Wiki 向量化)..."
  python scripts/ingest_all.py
else
  info "跳过数据灌入(--skip-ingest)"
fi

# ---- 5. 启动 Web 服务 ----
info "启动 FastAPI 服务: http://127.0.0.1:8000/"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 等待服务就绪
info "等待服务就绪..."
for i in $(seq 1 30); do
  if curl -s -o /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
    break
  fi
  sleep 1
done

# ---- 6. 冒烟测试 ----
if [ "$NO_SMOKE" = false ]; then
  info "运行冒烟测试..."
  python scripts/smoke_test.py
  SMOKE_EXIT=$?
else
  info "跳过冒烟测试(--no-smoke)"
  SMOKE_EXIT=0
fi

info "服务已启动: http://127.0.0.1:8000/  (Ctrl+C 停止)"
if [ "$SMOKE_EXIT" -ne 0 ]; then
  exit $SMOKE_EXIT
fi

# 前台等待(让用户手动 Ctrl+C 停止)
wait $SERVER_PID
