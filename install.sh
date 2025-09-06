#!/usr/bin/env bash
# Limbic Memory 依赖安装脚本 (install.sh)
# 作用：一键安装/更新 后端(Python) 与 前端(Node 子模块) 依赖。
# 无需传参；根据 requirements.txt / package-lock 等文件变化自动判断是否重新安装。
# 强制重装: 手动删除 .deps.ok / .deps.hash (后端或前端对应的) 再运行。
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$PROJECT_ROOT/.venv"
REQ_FILE="$PROJECT_ROOT/requirements.txt"
PY_HASH_FILE="$PROJECT_ROOT/.deps.hash"
PY_STAMP="$PROJECT_ROOT/.deps.ok"

FRONTEND_DIR="$PROJECT_ROOT/external/nemo-agent-toolkit-ui"
FRONT_HASH_FILE="$FRONTEND_DIR/.deps.hash"
FRONT_STAMP="$FRONTEND_DIR/.deps.ok"

PYTHON_BIN=python3
PY_MIN_MAJOR=3
PY_MIN_MINOR=8
NODE_MIN_VERSION=18

# 可配置: 离线模式 (1 = 不尝试联网安装)
OFFLINE_FLAG=${LM_OFFLINE:-0}
PIP_TIMEOUT=${LM_PIP_TIMEOUT:-25}
NPM_TIMEOUT=${LM_NPM_TIMEOUT:-30}
PIP_RETRIES=${LM_PIP_RETRIES:-2}
NPM_REGISTRY_MIRROR=${LM_NPM_MIRROR:-https://registry.npmmirror.com}

log(){ printf "[%s] %s\n" "$(date +'%H:%M:%S')" "$*"; }
section(){ echo -e "\n==== $* ===="; }
check_cmd(){ command -v "$1" >/dev/null 2>&1; }
hash_file(){ [ -f "$1" ] && sha1sum "$1" | awk '{print $1}' || true; }

have_timeout(){ command -v timeout >/dev/null 2>&1; }

network_ok(){
  [ "$OFFLINE_FLAG" = "1" ] && return 1
  # 先探测 PyPI / 备用镜像 / npm registry 任一成功即认为在线
  local targets=("https://pypi.org/simple" "https://pypi.tuna.tsinghua.edu.cn/simple" "$NPM_REGISTRY_MIRROR")
  for t in "${targets[@]}"; do
    if curl -k --connect-timeout 5 -m 8 -sI "$t" >/dev/null 2>&1; then return 0; fi
  done
  return 1
}

auto_pip_install(){
  local req="$1"; local try_mirror=0; local base_cmd="pip install --retries $PIP_RETRIES --default-timeout $PIP_TIMEOUT -r $req"
  local run_cmd="$base_cmd"
  if have_timeout; then run_cmd="timeout 300 $run_cmd"; fi
  eval $run_cmd && return 0 || try_mirror=1
  if [ $try_mirror -eq 1 ]; then
    log "pip 默认源失败，尝试使用清华镜像..."
    run_cmd="$base_cmd -i https://pypi.tuna.tsinghua.edu.cn/simple"
    if have_timeout; then run_cmd="timeout 300 $run_cmd"; fi
    eval $run_cmd && return 0 || return 1
  fi
}

auto_npm_install(){
  local dir="$1"; local cmd=""; pushd "$dir" >/dev/null
  if [ -f pnpm-lock.yaml ] && check_cmd pnpm; then
    cmd="pnpm install"
  elif [ -f yarn.lock ] && check_cmd yarn; then
    cmd="yarn install --frozen-lockfile"
  elif [ -f package-lock.json ]; then
    cmd="npm ci"
  else
    cmd="npm install"
  fi
  log "执行: $cmd"
  if have_timeout; then
    if ! timeout 420 bash -c "$cmd"; then
      log "默认 registry 失败，尝试使用镜像 $NPM_REGISTRY_MIRROR...";
      if [[ "$cmd" == npm* ]]; then timeout 420 bash -c "$cmd --registry $NPM_REGISTRY_MIRROR" || true; else npm install --registry $NPM_REGISTRY_MIRROR || true; fi
    fi
  else
    if ! $cmd; then
      log "默认 registry 失败，尝试使用镜像 $NPM_REGISTRY_MIRROR...";
      if [[ "$cmd" == npm* ]]; then $cmd --registry $NPM_REGISTRY_MIRROR || true; else npm install --registry $NPM_REGISTRY_MIRROR || true; fi
    fi
  fi
  popd >/dev/null
}

section "后端(Python)"
if ! network_ok; then
  if [ "$OFFLINE_FLAG" = "1" ]; then
    log "离线模式: 跳过后端依赖安装 (已设置 LM_OFFLINE=1)"
  else
    log "未检测到可用网络 (可设置 LM_OFFLINE=1 明确离线)。将跳过依赖安装。"
  fi
fi
if ! check_cmd "$PYTHON_BIN"; then echo "❌ 未找到 python3 (>=${PY_MIN_MAJOR}.${PY_MIN_MINOR})"; exit 1; fi
PY_VER=$($PYTHON_BIN -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 - <<EOF || echo "⚠️ Python ${PY_VER} 低于推荐 ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+"
import sys
maj,mi=map(int,"$PY_VER".split('.'))
sys.exit(0 if (maj>${PY_MIN_MAJOR} or (maj==${PY_MIN_MAJOR} and mi>=${PY_MIN_MINOR})) else 1)
EOF

if [ ! -d "$VENV_DIR" ]; then
  log "创建虚拟环境 $VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
else
  log "复用虚拟环境"
fi
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools >/dev/null 2>&1 || true

if [ -f "$REQ_FILE" ] && network_ok; then
  NEW_HASH=$(hash_file "$REQ_FILE")
  OLD_HASH=""; [ -f "$PY_HASH_FILE" ] && OLD_HASH=$(cat "$PY_HASH_FILE") || true
  if [ ! -f "$PY_STAMP" ] || [ "$NEW_HASH" != "$OLD_HASH" ]; then
    section "安装/更新 后端依赖"
    if auto_pip_install "$REQ_FILE"; then
      echo "$NEW_HASH" > "$PY_HASH_FILE"; date > "$PY_STAMP"; log "✅ 后端依赖安装成功"
    else
      echo "❌ 后端依赖安装失败，请检查网络或 requirements.txt"; deactivate || true; exit 2
    fi
  else
    log "后端依赖未变化，跳过"
  fi
else
  log "未找到 requirements.txt (跳过)"
fi
deactivate || true

# 生成本地 backend/config.json (若不存在) 避免把真实密钥提交仓库
BACKEND_DIR="$PROJECT_ROOT/backend"
CFG_EXAMPLE="$BACKEND_DIR/config.example.json"
CFG_FILE="$BACKEND_DIR/config.json"
if [ -d "$BACKEND_DIR" ]; then
  if [ -f "$CFG_EXAMPLE" ] && [ ! -f "$CFG_FILE" ]; then
    cp "$CFG_EXAMPLE" "$CFG_FILE"
    log "已生成本地 backend/config.json (请编辑填入真实 api_key)"
  fi
fi

section "前端(Node)"
if ! network_ok; then
  log "离线/无网络: 跳过前端依赖安装"
fi
if [ ! -d "$FRONTEND_DIR" ]; then
  log "未找到前端目录 $FRONTEND_DIR (如为子模块请: git submodule update --init --recursive)"
else
  if ! check_cmd node || ! check_cmd npm; then
    log "⚠️ 未检测到 node/npm (需 Node >=${NODE_MIN_VERSION})，跳过前端部分"
  else
    NODE_VER=$(node -v | sed 's/^v//'); NODE_MAJOR=${NODE_VER%%.*}
    if [ "$NODE_MAJOR" -lt "$NODE_MIN_VERSION" ]; then log "⚠️ Node 版本低于推荐 $NODE_MIN_VERSION"; fi
    if [ -f "$FRONTEND_DIR/package-lock.json" ]; then
      LOCK_FILE="$FRONTEND_DIR/package-lock.json"
    elif [ -f "$FRONTEND_DIR/pnpm-lock.yaml" ]; then
      LOCK_FILE="$FRONTEND_DIR/pnpm-lock.yaml"
    elif [ -f "$FRONTEND_DIR/yarn.lock" ]; then
      LOCK_FILE="$FRONTEND_DIR/yarn.lock"
    else
      LOCK_FILE="$FRONTEND_DIR/package.json"
    fi
    F_NEW_HASH=$(hash_file "$LOCK_FILE")
    F_OLD_HASH=""; [ -f "$FRONT_HASH_FILE" ] && F_OLD_HASH=$(cat "$FRONT_HASH_FILE") || true
    if [ ! -f "$FRONT_STAMP" ] || [ "$F_NEW_HASH" != "$F_OLD_HASH" ]; then
      section "安装/更新 前端依赖"
      if network_ok; then
        auto_npm_install "$FRONTEND_DIR"
        echo "$F_NEW_HASH" > "$FRONT_HASH_FILE"; date > "$FRONT_STAMP"; log "✅ 前端依赖安装成功"
      else
        log "跳过 (无网络)"
      fi
    else
      log "前端依赖未变化，跳过"
    fi
  fi
fi

section "完成"
echo "Python 虚拟环境: $VENV_DIR (激活: source .venv/bin/activate)"
echo "统一启动: ./start.sh  (自动选择后端端口, 生成前端 .env.local, 同时拉起前后端)"
echo "仅启动后端(调试): uvicorn backend.main:app --port 8100 --reload"
echo "仅启动前端(调试): cd external/nemo-agent-toolkit-ui && npm run dev"
echo "配置文件: backend/config.json (请编辑填入真实 api_key)"
echo "强制重装依赖: 删除 .deps.ok / .deps.hash 后执行 ./install.sh"
echo "说明: install.sh 只做依赖与配置生成, 运行流程请用 start.sh"