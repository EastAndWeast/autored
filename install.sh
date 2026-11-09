#!/usr/bin/env bash
# =============================================================
# 小红书/Rednote AI 运营助手 — 一键安装脚本 (macOS / Linux)
#
# 三种用法:
#   1) 一句话安装(脚本不在仓库内时自动克隆到 ~/autored):
#        curl -fsSL https://raw.githubusercontent.com/EastAndWeast/autored/main/install.sh | bash
#      自定义安装位置:  curl -fsSL <同上URL> | XHS_HOME=/目标路径 bash
#   2) 仓库内执行:  bash install.sh
#   3) --start  安装完成后立即后台启动服务
# =============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

REPO_URL="https://github.com/EastAndWeast/autored.git"

# ---- 定位项目:支持「一句话/curl 直装」(脚本不在仓库内时自动克隆) ----
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
if [ ! -f "$SELF_DIR/app/main.py" ]; then
  TARGET="${XHS_HOME:-$HOME/autored}"
  command -v git >/dev/null 2>&1 \
    || fail "未找到 git。macOS: 运行 xcode-select --install 后重试;Linux: sudo apt install git"
  if [ -f "$TARGET/app/main.py" ]; then
    info "检测到已装目录 $TARGET,更新代码…"
    git -C "$TARGET" pull --ff-only >/dev/null 2>&1 || warn "更新失败,使用现有版本继续"
  else
    warn "首次安装:克隆仓库到 $TARGET …"
    git clone --depth 1 "$REPO_URL" "$TARGET" \
      || fail "克隆失败,请检查网络后重试(也可手动下载仓库 zip 解压后运行 install.sh)"
  fi
  cd "$TARGET"
else
  cd "$SELF_DIR"
fi
PROJECT_DIR="$(pwd -P)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
WB_DIR="$PROJECT_DIR/skills/workbuddy"

echo ""
echo "=========================================="
echo "  小红书 AI 运营助手 · 一键安装"
echo "=========================================="
echo "项目目录: $PROJECT_DIR"
echo ""

# ---------- 0. 平台检查 ----------
case "$(uname -s)" in
  MINGW*|CYGWIN*|MSYS*)
    fail "检测到 Windows 环境:请改用项目自带的 install.bat(双击或在 PowerShell 中运行)"
    ;;
esac

# ---------- 1. Python ----------
PY=python3
if ! command -v $PY >/dev/null 2>&1; then
  fail "未找到 python3,请先安装 Python 3.9+ (macOS: brew install python3)"
fi
PYVER=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
$PY - "$PYVER" <<'EOF'
import sys
v = sys.argv[1].split(".")
num = int(v[0]) * 100 + int(v[1])
# 支持范围与 packaging/RELEASE_MANIFEST.json 一致: >=3.9,<3.14(CI 已验证 3.13)
sys.exit(0 if 309 <= num < 314 else 1)
EOF
if [ $? -ne 0 ]; then
  if [[ "${ALLOW_UNSUPPORTED_PYTHON:-}" == "1" ]]; then
    warn "Python $PYVER 不在官方支持范围(3.9~3.13),ALLOW_UNSUPPORTED_PYTHON=1 已强制继续"
  else
    fail "Python $PYVER 不受支持(需要 3.9 ~ 3.13)。可安装 3.12/3.13 后重试,或设置 ALLOW_UNSUPPORTED_PYTHON=1 强制继续"
  fi
fi
info "Python $PYVER"

# ---------- 2. 虚拟环境 ----------
if [ ! -x "$VENV_PYTHON" ]; then
  warn "创建虚拟环境…"
  $PY -m venv venv
fi
info "虚拟环境就绪"

# ---------- 3. pip 依赖(国内网络自动切清华镜像) ----------
PIP_ARGS=("-r" "requirements.txt" "--quiet")
if curl -s --max-time 4 https://pypi.org >/dev/null 2>&1; then
  info "使用官方 PyPI 源"
else
  PIP_ARGS+=("-i" "https://pypi.tuna.tsinghua.edu.cn/simple")
  info "检测到国内网络,使用清华 PyPI 镜像"
fi
warn "安装 Python 依赖(首次约 1-2 分钟)…"
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install "${PIP_ARGS[@]}"
info "Python 依赖安装完成"

# ---------- 4. Chromium(国内自动用 npmmirror 镜像) ----------
if "$VENV_PYTHON" -c "from patchright.sync_api import sync_playwright" >/dev/null 2>&1 \
   && [ -d "$HOME/Library/Caches/ms-playwright" -o -d "$HOME/.cache/ms-playwright" ] \
   && ls "$HOME/Library/Caches/ms-playwright" "$HOME/.cache/ms-playwright" 2>/dev/null | grep -q chromium; then
  info "Chromium 已存在,跳过下载"
else
  warn "下载 Chromium(约 150MB,首次必下)…"
  if curl -s --max-time 4 https://cdn.playwright.dev >/dev/null 2>&1; then
    "$VENV_PYTHON" -m patchright install chromium
  else
    PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" \
      "$VENV_PYTHON" -m patchright install chromium
    info "已使用 npmmirror 镜像下载"
  fi
fi

# ---------- 5. 数据目录 ----------
mkdir -p data/uploads data/profiles
info "数据目录就绪 (data/)"

# ---------- 6. 生成 WorkBuddy connector 配置 ----------
render_connector() {
  sed -e "s|{{VENV_PYTHON}}|$VENV_PYTHON|g" -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
    "$WB_DIR/connector.template.json" > "$WB_DIR/connector.json"
  # 标准 MCP 格式(WorkBuddy/其他 MCP 客户端通用)
  cat > "$PROJECT_DIR/mcp.generated.json" <<EOF
{
  "mcpServers": {
    "xhs-assistant": {
      "command": "$VENV_PYTHON",
      "args": ["-m", "app.mcp.server"],
      "cwd": "$PROJECT_DIR",
      "env": { "PYTHONUNBUFFERED": "1" }
    }
  }
}
EOF
}
render_connector
info "已生成 skills/workbuddy/connector.json 与 mcp.generated.json"

# ---------- 6.5 自动注册到 WorkBuddy(安全合并;已存在则不动) ----------
warn "注册到 WorkBuddy (~/.workbuddy/mcp.json)…"
"$VENV_PYTHON" - "$PROJECT_DIR" <<'WBEOF'
import json, shutil, sys
from pathlib import Path

proj = sys.argv[1]
server = {
    "command": str(Path(proj) / "venv/bin/python"),
    "args": ["-m", "app.mcp.server"],
    "cwd": proj,
    "env": {"PYTHONUNBUFFERED": "1"},
}
wb = Path.home() / ".workbuddy"
cfg = wb / "mcp.json"
wb.mkdir(parents=True, exist_ok=True)

data = {"mcpServers": {}}
if cfg.exists():
    shutil.copy2(cfg, str(cfg) + ".bak")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        print(f"[!] 已有 {cfg} 不是合法 JSON,已跳过自动注册(备份在 .bak)")
        sys.exit(0)

servers = data.setdefault("mcpServers", {})
if "xhs-assistant" in servers:
    print("[i] WorkBuddy 配置中已存在 xhs-assistant,保持原样未覆盖")
else:
    servers["xhs-assistant"] = server
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[✓] 已注册到 {cfg}(原文件备份为 .bak)")
WBEOF
info "别忘了:在 WorkBuddy「连接器管理 → 自定义连接器」里对新出现的 xhs-assistant 点「信任」"

# ---------- 7. 可选:立即启动 ----------
if [[ "${1:-}" == "--start" ]]; then
  if lsof -iTCP:8643 -sTCP:LISTEN >/dev/null 2>&1; then
    info "服务已在运行 (http://127.0.0.1:8643)"
  else
    nohup "$VENV_PYTHON" run.py > data/service.log 2>&1 &
    sleep 3
    if curl -s --max-time 5 http://127.0.0.1:8643/api/health | grep -q ok; then
      info "服务已后台启动 → http://127.0.0.1:8643"
    else
      warn "服务启动较慢或失败,查看 data/service.log"
    fi
  fi
fi

# ---------- 完成 ----------
echo ""
echo "=========================================="
echo -e "${GREEN}安装完成!${NC}"
echo "=========================================="
echo ""
echo "下一步:"
echo "  1) 把本地服务接进 WorkBuddy —— 在 WorkBuddy 的 MCP 设置中添加,内容用:"
echo "       $PROJECT_DIR/mcp.generated.json"
echo "     (或手动填 command=$VENV_PYTHON  args=-m app.mcp.server  cwd=$PROJECT_DIR)"
echo "  2) 在 WorkBuddy 对话里说「启动登录」,然后打开 http://127.0.0.1:8643 用小红书 APP 扫码"
echo "  3) 扫码成功后即可对话:写草稿 → 确认发布 → 查数据"
echo ""
echo "网页管理后台: $VENV_PYTHON run.py  → http://127.0.0.1:8643"
echo ""
