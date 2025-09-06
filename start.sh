#!/usr/bin/env bash
# 启动脚本：同时启动后端 (FastAPI) + 前端 (Next.js UI)
# 只负责运行，不做依赖安装逻辑；若检测到依赖缺失会自动调用 ./install.sh
# 停止：按 Ctrl+C 会自动优雅终止两个进程。
set -euo pipefail

# 可选后台模式：参数 -d 或环境变量 START_DETACH=1
DETACH=0
if [ "${1:-}" = "-d" ] || [ "${START_DETACH:-0}" = "1" ]; then
  DETACH=1
fi

PROJECT_ROOT=$(cd "$(dirname "$0")" && pwd)
BACKEND_APP="backend.main:app"
# 后端基础起始端口改为 8100（避免与本机已有 8000 占用冲突）
BACKEND_BASE_PORT=8100
FRONTEND_DIR="$PROJECT_ROOT/external/nemo-agent-toolkit-ui"
FRONTEND_BASE_PORT=3000
INSTALL_SCRIPT="$PROJECT_ROOT/install.sh"
VENV_DIR="$PROJECT_ROOT/.venv"
LOG_DIR="$PROJECT_ROOT/.runtime"
mkdir -p "$LOG_DIR"
BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"

log(){ printf "[%s] %s\n" "$(date +'%H:%M:%S')" "$*"; }
err(){ printf "\e[31m[%s] %s\e[0m\n" "$(date +'%H:%M:%S')" "$*" >&2; }

log "启动脚本初始化..."

# 优雅结束进程
graceful_kill(){
  local pid="$1"; local name="$2"; local t=0
  [ -z "$pid" ] && return 0
  if ! kill -0 "$pid" >/dev/null 2>&1; then return 0; fi
  kill "$pid" 2>/dev/null || true
  while kill -0 "$pid" >/dev/null 2>&1 && [ $t -lt 30 ]; do
    sleep 0.1; t=$((t+1))
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -9 "$pid" 2>/dev/null || true
    log "已强制结束残留进程 $name ($pid)"
  else
    log "已结束 $name ($pid)"
  fi
}

cleanup_stale(){
  if [ -f "$BACKEND_PID_FILE" ]; then
    local opid=$(cat "$BACKEND_PID_FILE" 2>/dev/null || true)
    [ -n "$opid" ] && graceful_kill "$opid" backend_prev || true
    rm -f "$BACKEND_PID_FILE" || true
  fi
  if [ -f "$FRONTEND_PID_FILE" ]; then
    local fpid=$(cat "$FRONTEND_PID_FILE" 2>/dev/null || true)
    [ -n "$fpid" ] && graceful_kill "$fpid" frontend_prev || true
    rm -f "$FRONTEND_PID_FILE" || true
  fi
  if command -v ps >/dev/null 2>&1; then
    # 后端扫描 (忽略无匹配错误)
    { ps -eo pid,command | grep -F "$PROJECT_ROOT" | grep -E 'uvicorn .*backend.main:app' | grep -v grep || true; } \
      | awk '{print $1}' | while read -r p; do [ -n "$p" ] && graceful_kill "$p" backend_scan || true; done
    # 前端扫描
    { ps -eo pid,command | grep -F "$FRONTEND_DIR" | grep -E 'node .*next' | grep -v grep || true; } \
      | awk '{print $1}' | while read -r p; do [ -n "$p" ] && graceful_kill "$p" frontend_scan || true; done
  fi
}

cleanup_stale

need_install=0
if [ ! -d "$VENV_DIR" ] || [ ! -f "$PROJECT_ROOT/.deps.ok" ]; then need_install=1; fi
if [ -d "$FRONTEND_DIR" ]; then
  if [ ! -d "$FRONTEND_DIR/node_modules" ] || [ ! -f "$FRONTEND_DIR/.deps.ok" ]; then need_install=1; fi
else
  err "未找到前端目录 $FRONTEND_DIR (子模块可能未初始化)"
fi
if [ $need_install -eq 1 ]; then
  if [ -x "$INSTALL_SCRIPT" ]; then log "检测到依赖未安装或不完整，自动执行 install.sh ..."; bash "$INSTALL_SCRIPT"; else err "缺少 install.sh，无法自动安装依赖。"; fi
fi

if [ ! -d "$VENV_DIR" ]; then err "虚拟环境不存在，请先运行 ./install.sh"; exit 1; fi
source "$VENV_DIR/bin/activate"
if ! command -v uvicorn >/dev/null 2>&1; then err "uvicorn 未安装，请运行 ./install.sh"; deactivate || true; exit 1; fi

find_free_port(){
  local start=$1; local limit=${2:-20}; local p=$start; local i=0
  while [ $i -lt $limit ]; do
    if command -v lsof >/dev/null 2>&1; then
      if ! lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then echo $p; return 0; fi
    elif command -v ss >/dev/null 2>&1; then
      if ! ss -ltn | awk '{print $4}' | grep -q ":$p$"; then echo $p; return 0; fi
    else
      if (echo > /dev/tcp/127.0.0.1/$p) >/dev/null 2>&1; then :; else echo $p; return 0; fi
    fi
    p=$((p+1)); i=$((i+1))
  done
  return 1
}

is_port_in_use(){
  local p=$1
  if command -v lsof >/dev/null 2>&1; then lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
  elif command -v ss >/dev/null 2>&1; then ss -ltn | awk '{print $4}' | grep -q ":$p$" && return 0 || return 1
  else (echo > /dev/tcp/127.0.0.1/$p) >/dev/null 2>&1 && return 0 || return 1; fi
}

start_frontend(){
  local attempts=15; local port=$FRONTEND_PORT; local i=0
  while [ $i -lt $attempts ]; do
    if is_port_in_use "$port"; then log "端口 $port 被占用，尝试下一个..."; port=$((port+1)); i=$((i+1)); continue; fi
    log "启动前端 (尝试端口 $port)..."
    (cd "$FRONTEND_DIR" && PORT="$port" npm run dev >"$FRONTEND_LOG" 2>&1 & echo $! > "$FRONTEND_PID_FILE")
    local pid=$(cat "$FRONTEND_PID_FILE" 2>/dev/null || true)
    sleep 2
    if kill -0 "$pid" >/dev/null 2>&1; then FRONTEND_PORT=$port; FRONTEND_PID=$pid; log "前端进程 PID=$FRONTEND_PID (端口: $FRONTEND_PORT 日志: $FRONTEND_LOG)"; return 0; fi
    if grep -q 'EADDRINUSE' "$FRONTEND_LOG" 2>/dev/null; then log "检测到 EADDRINUSE 日志，端口 $port 不可用 -> 重试下一端口"; port=$((port+1)); i=$((i+1)); continue; else err "前端启动失败 (前 40 行)"; head -n 40 "$FRONTEND_LOG"; return 1; fi
  done
  err "前端端口连续冲突/失败，放弃启动"; return 1
}

BACKEND_PORT=$(find_free_port $BACKEND_BASE_PORT 10 || echo $BACKEND_BASE_PORT)
FRONTEND_PORT=$(find_free_port $FRONTEND_BASE_PORT 20 || echo $FRONTEND_BASE_PORT)
log "后端使用端口: $BACKEND_PORT"; log "前端期望端口: $FRONTEND_PORT"

generate_frontend_env(){
  local target="$FRONTEND_DIR/.env.local"; [ ! -d "$FRONTEND_DIR" ] && return 0
  # 若存在固定端口的 .env 可能覆盖 .env.local，备份后移除以避免端口不一致 (8000 vs 动态端口)
  if [ -f "$FRONTEND_DIR/.env" ]; then
    if grep -q '127.0.0.1:8000' "$FRONTEND_DIR/.env"; then
      mv "$FRONTEND_DIR/.env" "$FRONTEND_DIR/.env.bak" && log "已备份并移除固定端口 .env -> .env.bak (改用动态 .env.local)"
    fi
  fi
  # 之前版本把 \n 作为普通字符写进单行，导致 NEXT_PUBLIC_WORKFLOW 变量的值包含后续整个文件内容，
  # 页面上就会显示出后续环境变量名 (例如 NEXT_PUBLIC_WEBSOCKET_CHAT_COMPLETION_URL)。
  # 这里改成真正的多行写入，避免串行污染。
  local new_content
  new_content=$(cat <<EOF
NEXT_PUBLIC_WORKFLOW=NeMo Agent Toolkit
NEXT_PUBLIC_WEBSOCKET_CHAT_COMPLETION_URL=ws://127.0.0.1:${BACKEND_PORT}/websocket
NEXT_PUBLIC_HTTP_CHAT_COMPLETION_URL=http://127.0.0.1:${BACKEND_PORT}/chat/stream
NEXT_PUBLIC_WEB_SOCKET_DEFAULT_ON=false
NEXT_PUBLIC_CHAT_HISTORY_DEFAULT_ON=false
NEXT_PUBLIC_RIGHT_MENU_OPEN=false
EOF
)
  if [ -f "$target" ] && diff -q <(printf "%s" "$new_content") "$target" >/dev/null 2>&1; then
    log ".env.local 未变化"
  else
    printf "%s\n" "$new_content" > "$target"
    log "已生成/更新前端动态环境文件: $target"
  # 删除旧的缓存环境文件与构建产物，避免继续读取已污染的 window.__ENV / 编译缓存
  if [ -f "$FRONTEND_DIR/public/__ENV.js" ]; then rm -f "$FRONTEND_DIR/public/__ENV.js" && log "已删除旧的 public/__ENV.js"; fi
  if [ -d "$FRONTEND_DIR/.next" ]; then rm -rf "$FRONTEND_DIR/.next" && log "已清理前端 .next 缓存 (将触发重新编译)"; fi
  fi
}

BACKEND_LOG="$LOG_DIR/backend.log"; FRONTEND_LOG="$LOG_DIR/frontend.log"; rm -f "$BACKEND_LOG" "$FRONTEND_LOG" || true

log "启动后端 (无热重载)..."; uvicorn "$BACKEND_APP" --host 0.0.0.0 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 & BACKEND_PID=$!; echo "$BACKEND_PID" > "$BACKEND_PID_FILE"; log "后端进程 PID=$BACKEND_PID (日志: $BACKEND_LOG)"

FRONTEND_PID=""
if [ -d "$FRONTEND_DIR" ] && command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then generate_frontend_env; start_frontend || true; else err "前端未启动（缺少目录或 node/npm）"; fi

health_check(){ for _ in $(seq 1 20); do if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/healthz" >/dev/null 2>&1; then log "后端健康检查通过: http://127.0.0.1:${BACKEND_PORT}/healthz"; return 0; fi; sleep 0.5; done; err "后端健康检查失败 (仍可查看日志)"; }
health_check || true

log "运行中："; echo "  后端 API:   http://localhost:${BACKEND_PORT} (健康: /healthz)"; if [ -n "$FRONTEND_PID" ]; then echo "  前端 UI:    http://localhost:${FRONTEND_PORT}"; else echo "  前端 UI:    未启动"; fi; echo "  日志目录:   $LOG_DIR"; echo "  停止: Ctrl+C"

cleanup(){
  log "捕获退出信号，正在停止进程..."
  [ -n "${FRONTEND_PID:-}" ] && graceful_kill "$FRONTEND_PID" frontend || true
  [ -n "${BACKEND_PID:-}" ] && graceful_kill "$BACKEND_PID" backend || true
  wait "$BACKEND_PID" 2>/dev/null || true
  [ -n "${FRONTEND_PID:-}" ] && wait "$FRONTEND_PID" 2>/dev/null || true
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" 2>/dev/null || true
  log "已退出"
}
if [ $DETACH -eq 0 ]; then
  trap cleanup INT TERM EXIT
else
  log "后台模式: 退出脚本时不会自动清理进程 (自行使用 kill 停止)"
fi

sleep 0.8
if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then err "后端进程已退出，日志如下"; tail -n 40 "$BACKEND_LOG" || true; exit 1; fi

# 监控循环：避免脚本自身提前结束触发 trap 进而杀掉后端
if [ $DETACH -eq 1 ]; then
  log "DETACH 模式: 后端 PID=$BACKEND_PID 前端 PID=${FRONTEND_PID:-N/A}"
  log "访问: http://localhost:${FRONTEND_PORT:-?}  后端: http://localhost:$BACKEND_PORT"
  log "停止: kill $BACKEND_PID ${FRONTEND_PID:-}"
  exit 0
fi

while true; do
  local_dead=0
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then err "后端已退出"; local_dead=1; fi
  if [ -n "${FRONTEND_PID:-}" ] && ! kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then log "前端已退出"; FRONTEND_PID=""; fi
  if [ $local_dead -eq 1 ]; then
    err "退出监控循环 (后端结束)"; tail -n 60 "$BACKEND_LOG" || true; break
  fi
  sleep 2
done