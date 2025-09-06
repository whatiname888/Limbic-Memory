#!/usr/bin/env bash
# Limbic Memory 项目启动 / 环境初始化脚本
# 功能: 依赖检查 -> (国内镜像判断) -> 创建/复用虚拟环境 -> 安装依赖 -> 给出进入环境提示
# 用法:
#   ./start.sh              # 初始化并安装依赖
#   ./start.sh --cn         # 强制使用清华镜像
#   ./start.sh --reinstall  # 重新安装依赖 (删除已安装标记)
#   ./start.sh --only-env   # 只创建虚拟环境, 不安装依赖
#   ./start.sh --help       # 查看说明
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")" && pwd)
VENV_DIR="$PROJECT_ROOT/.venv"
REQ_FILE="$PROJECT_ROOT/requirements.txt"
STAMP="$PROJECT_ROOT/.deps.ok"
FORCE_CN=0
REINSTALL=0
ONLY_ENV=0
PYTHON_BIN=python3
PY_MIN_MAJOR=3
PY_MIN_MINOR=8

print_help(){ grep '^# ' "$0" | sed 's/^# //'; }

for arg in "$@"; do
  case "$arg" in
    --cn) FORCE_CN=1;;
    --reinstall) REINSTALL=1;;
    --only-env) ONLY_ENV=1;;
    --help|-h) print_help; exit 0;;
    *) echo "未知参数: $arg"; print_help; exit 1;;
  esac
done

log(){ printf "[%s] %s\n" "$(date +'%H:%M:%S')" "$*"; }
section(){ echo -e "\n==== $* ===="; }

check_cmd(){ command -v "$1" >/dev/null 2>&1; }

section "依赖检查"
if ! check_cmd "$PYTHON_BIN"; then
  echo "❌ 未找到python3 (需要 >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR})"; exit 1; fi
PY_VER=$($PYTHON_BIN -c 'import sys;print("%d.%d"%sys.version_info[:2])')
PY_MAJOR=${PY_VER%.*}; PY_MINOR=${PY_VER#*.}
python3 - <<EOF || echo "⚠️  当前Python版本(${PY_VER}) 低于推荐 (${PY_MIN_MAJOR}.${PY_MIN_MINOR}+), 可能出现兼容性问题"
import sys
maj,minor=map(int,"$PY_VER".split('.'))
if maj<$PY_MIN_MAJOR or (maj==$PY_MIN_MAJOR and minor<$PY_MIN_MINOR):
    sys.exit(1)
EOF

if ! check_cmd git; then echo "❌ 缺少 git"; exit 1; fi
log "Python: $PY_VER  Git: $(git --version | cut -d' ' -f3)"

section "网络镜像检测"
PIP_INDEX=""
if [ $FORCE_CN -eq 1 ]; then
  log "已指定 --cn -> 使用清华镜像"
  PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
else
  if timeout 3 curl -fsSL https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1; then
    # 简单策略: 如果访问 pypi.org 明显慢/失败则用清华
    if ! timeout 3 curl -fsSL https://pypi.org/simple >/dev/null 2>&1; then
      PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
    fi
  fi
fi
[ -n "$PIP_INDEX" ] && log "使用镜像: $PIP_INDEX" || log "使用默认PyPI"

section "创建/复用虚拟环境"
if [ ! -d "$VENV_DIR" ]; then
  log "创建虚拟环境 $VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
else
  log "复用已有虚拟环境"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "虚拟环境 Python: $(python -V 2>&1)"
python -m pip install --upgrade pip wheel ${PIP_INDEX:+-i $PIP_INDEX}

section "准备 requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
  cat > "$REQ_FILE" <<'EOF'
# Limbic Memory 基础依赖 (初始占位, 可按模块演进补充)
# 记忆检索 / 向量
faiss-cpu>=1.7.4; platform_system!='Windows'
# Windows 可改用: faiss-windows
# LLM 客户端 (可选)
openai>=1.0.0
# 嵌入/模型生态
transformers>=4.40.0
# 数据处理
numpy>=1.24.0
pydantic>=2.5.0
uvloop; platform_system=='Linux'
loguru>=0.7.2
EOF
  log "生成默认 requirements.txt"
else
  log "检测到已有 requirements.txt"
fi

section "安装依赖"
if [ $REINSTALL -eq 1 ]; then rm -f "$STAMP"; fi
if [ $ONLY_ENV -eq 1 ]; then
  log "--only-env 指定: 跳过依赖安装"
elif [ ! -f "$STAMP" ]; then
  pip install -r "$REQ_FILE" ${PIP_INDEX:+-i $PIP_INDEX}
  date > "$STAMP"
  log "✅ 依赖安装完成"
else
  log "已安装 (使用 --reinstall 可重新安装)"
fi

section "完成"
echo "进入环境: source .venv/bin/activate"
echo "退出环境: deactivate"
echo "可编辑 requirements.txt 后运行: ./start.sh --reinstall"
