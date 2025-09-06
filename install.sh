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

log(){ printf "[%s] %s\n" "$(date +'%H:%M:%S')" "$*"; }
section(){ echo -e "\n==== $* ===="; }
check_cmd(){ command -v "$1" >/dev/null 2>&1; }
hash_file(){ [ -f "$1" ] && sha1sum "$1" | awk '{print $1}' || true; }

auto_pip_install(){
  local req="$1"; local try_mirror=0
  pip install -r "$req" && return 0 || try_mirror=1
  if [ $try_mirror -eq 1 ]; then
    log "pip 默认源失败，尝试使用清华镜像..."
    pip install -r "$req" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0 || return 1
  fi
}

auto_npm_install(){
  local dir="$1"; local cmd="";
  pushd "$dir" >/dev/null
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
  if $cmd; then popd >/dev/null; return 0; fi
  log "默认 registry 失败，尝试使用国内镜像..."
  if [[ "$cmd" == npm* ]]; then
    if npm install --registry https://registry.npmmirror.com; then popd >/dev/null; return 0; fi
  else
    log "兜底使用 npm + 国内镜像"
    if npm install --registry https://registry.npmmirror.com; then popd >/dev/null; return 0; fi
  fi
  popd >/dev/null; return 1
}

section "后端(Python)"
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

if [ -f "$REQ_FILE" ]; then
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

section "前端(Node)"
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
      if auto_npm_install "$FRONTEND_DIR"; then
        echo "$F_NEW_HASH" > "$FRONT_HASH_FILE"; date > "$FRONT_STAMP"; log "✅ 前端依赖安装成功"
      else
        log "❌ 前端依赖安装失败"
      fi
    else
      log "前端依赖未变化，跳过"
    fi
  fi
fi

section "完成"
echo "Python 虚拟环境: $VENV_DIR (激活: source .venv/bin/activate)"
echo "后端启动: ./run_backend.sh (FastAPI 开发模式端口 8000)"
echo "前端开发: cd external/nemo-agent-toolkit-ui && npm run dev"
echo "强制重装: 删除 .deps.ok / .deps.hash 后再次执行 ./install.sh"
echo "安装脚本仅负责依赖安装，不做运行编排。"