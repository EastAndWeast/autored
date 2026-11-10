#!/usr/bin/env python3
"""把本地 MCP 服务注册到 WorkBuddy 的 ~/.workbuddy/mcp.json(安全合并)。

由安装脚本调用(install.sh / install.bat),也可手动运行:
    python wb_register.py <项目目录>

行为约定:
- 已存在同名服务 xhs-assistant → 保持原样,不覆盖
- 真正改动前把原文件备份为 .bak
- 已有文件不是合法 JSON → 跳过注册并提示(兼容带 BOM 的文件)
- 写出文件为 UTF-8 无 BOM
- 控制台输出只用 ASCII 标签,避免 Windows 控制台编码问题
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SERVER_NAME = "xhs-assistant"


def main() -> int:
    proj = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    py = (
        Path(proj) / "venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else Path(proj) / "venv" / "bin" / "python"
    )
    server = {
        "command": str(py),
        "args": ["-m", "app.mcp.server"],
        "cwd": str(proj),
        "env": {"PYTHONUNBUFFERED": "1"},
    }

    wb = Path.home() / ".workbuddy"
    cfg = wb / "mcp.json"
    wb.mkdir(parents=True, exist_ok=True)

    existed_before = cfg.exists()

    data: dict = {"mcpServers": {}}
    if existed_before:
        try:
            # utf-8-sig 兼容带 BOM 的旧文件(如 PowerShell 5.1 写出的)
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except Exception:
            print(f"[!] {cfg} is not valid JSON, skipped auto-register.")
            print("    Please merge mcp.generated.json manually.")
            return 1
        # 合法 JSON 但顶层不是对象(如 [1,2,3])同样归入跳过,避免 AttributeError
        if not isinstance(data, dict):
            print(f"[!] {cfg} top-level is not a JSON object, skipped auto-register.")
            print("    Please merge mcp.generated.json manually.")
            return 1

    servers = data.setdefault("mcpServers", {})
    if SERVER_NAME in servers:
        print(f"[i] {SERVER_NAME} already exists in {cfg}, kept as-is.")
        return 0

    if existed_before:
        shutil.copy2(cfg, str(cfg) + ".bak")  # 只在真正改动前且原文件存在时备份

    servers[SERVER_NAME] = server
    cfg.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if existed_before:
        print(f"[OK] registered to {cfg} (previous file saved as .bak)")
    else:
        # 原文件不存在:没有任何备份可回滚,据实提示
        print(f"[OK] registered to {cfg} (no previous config, nothing to back up)")
    print("[i] Remember to trust 'xhs-assistant' in WorkBuddy connector settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
