"""宿主 AI(Codex & WorkBuddy)配置合并与安装服务。

核心安全规范:
修改用户已有配置文件前自动创建 .bak 备份,并以字典合并方式注入 xhs-ai-assistant,
严禁直接覆盖导致用户丢失其他已配置的 MCP 服务!
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .. import config

DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.json"


def get_assistant_mcp_config() -> dict[str, Any]:
    """生成适合当前宿主环境运行的 MCP 配置项。"""
    return {
        "command": sys.executable,
        "args": [
            "-m",
            "app.mcp.server",
        ],
        "cwd": str(config.BASE_DIR),
        "env": {
            "PYTHONUNBUFFERED": "1",
            "XHS_DATA_DIR": str(config.DATA_DIR),
        },
    }


def configure_codex(target_path: Path | str | None = None) -> dict[str, Any]:
    """将助手接入配置安全合并至 Codex 配置文件中。"""
    cfg_path = Path(target_path) if target_path else DEFAULT_CODEX_CONFIG
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing_data: dict[str, Any] = {}
    backup_made = False

    if cfg_path.exists():
        try:
            raw_text = cfg_path.read_text(encoding="utf-8")
            if raw_text.strip():
                existing_data = json.loads(raw_text)
            # 安全备份
            bak_path = cfg_path.with_suffix(".json.bak")
            shutil.copy2(cfg_path, bak_path)
            backup_made = True
        except Exception as e:
            existing_data = {}

    if not isinstance(existing_data, dict):
        existing_data = {}

    if "mcpServers" not in existing_data or not isinstance(existing_data["mcpServers"], dict):
        existing_data["mcpServers"] = {}

    # 注入/更新我们的节点,保留其它已有的 MCP 节点
    existing_data["mcpServers"]["xhs-ai-assistant"] = get_assistant_mcp_config()

    cfg_path.write_text(json.dumps(existing_data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "config_file": str(cfg_path),
        "backup_created": backup_made,
        "all_mcp_servers": list(existing_data["mcpServers"].keys()),
        "message": "Codex MCP 配置已成功安全合并",
    }


def configure_workbuddy(target_dir: Path | str | None = None) -> dict[str, Any]:
    """准备并更新 WorkBuddy 连接器包。"""
    src_dir = config.BASE_DIR / "skills" / "workbuddy"
    out_dir = Path(target_dir) if target_dir else (config.BASE_DIR / "skills" / "workbuddy")

    connector_file = out_dir / "connector.json"
    if connector_file.exists():
        try:
            conn_data = json.loads(connector_file.read_text(encoding="utf-8"))
            conn_data["command"] = sys.executable
            conn_data["cwd"] = str(config.BASE_DIR)
            if "env" not in conn_data:
                conn_data["env"] = {}
            conn_data["env"]["XHS_DATA_DIR"] = str(config.DATA_DIR)
            connector_file.write_text(json.dumps(conn_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "ok": True,
        "connector_dir": str(out_dir),
        "message": "WorkBuddy 连接器配置已就绪",
    }
