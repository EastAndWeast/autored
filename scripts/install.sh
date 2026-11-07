#!/usr/bin/env bash
# 小红书 AI 运营助手 - 干净环境一键安装与就绪初始化脚本 (macOS)
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=========================================================="
echo "    📕 小红书 AI 运营助手 (xhs-ai-assistant) 安装程序"
echo "=========================================================="

# 1. 检查操作系统
OS="$(uname -s)"
ARCH="$(uname -m)"
echo "▶ 操作系统: $OS ($ARCH)"
if [ "$OS" != "Darwin" ]; then
    echo "⚠️ 当前首发支持系统为 macOS，其它系统仍在移植适配中。"
fi

# 2. 检查 Python 版本 (需 >= 3.9)
PYTHON_CMD=""
for cmd in python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PY_VER="$("$cmd" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
        PY_MAJOR="$("$cmd" -c 'import sys; print(sys.version_info[0])')"
        PY_MINOR="$("$cmd" -c 'import sys; print(sys.version_info[1])')"
        if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 9 ]; then
            PYTHON_CMD="$cmd"
            echo "▶ 找到兼容的 Python: $cmd (版本 $PY_VER)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "❌ 错误: 未找到 Python 3.9 及以上版本。请先安装 Python 3.9+ 后再运行本安装脚本。"
    exit 1
fi

# 3. 初始化/更新专用虚拟环境
VENV_DIR="$PROJECT_ROOT/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "▶ 正在创建独立虚拟环境: $VENV_DIR ..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# 4. 安装锁定依赖
echo "▶ 正在安装锁定生产依赖 (requirements.lock)..."
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -r "$PROJECT_ROOT/requirements.lock" -q

# 5. 确保浏览器组件就绪
echo "▶ 正在检查并安装反检测浏览器组件 (patchright install chromium)..."
"$VENV_PYTHON" -m patchright install chromium

# 6. 初始化数据库与执行增量迁移
echo "▶ 正在初始化数据存储与数据库 Schema 迁移..."
"$VENV_PYTHON" -c 'from app.models import init_db; init_db()'

# 7. 运行自检
echo "▶ 正在运行系统环境自检 (doctor)..."
"$VENV_PYTHON" run.py doctor

echo "=========================================================="
echo "🎉 安装完成! 您可以通过以下命令开始使用:"
echo "   1. 启动服务:         ./venv/bin/python run.py start"
echo "   2. 打开管理网页:     ./venv/bin/python run.py open"
echo "   3. 接入 Codex:       ./venv/bin/python run.py configure codex"
echo "   4. 接入 WorkBuddy:   ./venv/bin/python run.py configure workbuddy"
echo "   5. 开机登录自启动:   ./venv/bin/python run.py service install"
echo "=========================================================="
