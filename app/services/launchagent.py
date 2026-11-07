"""macOS 用户级开机/登录常驻服务(LaunchAgent)管理器。

完全无需 root/sudo 权限,基于 macOS 原生 launchd 机制:
- 配置文件位置: ~/Library/LaunchAgents/com.xhs.assistant.plist
- 开机/用户登录时自动拉起后台服务
- 进程异常崩溃时 launchd 自动恢复(KeepAlive)
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .. import config

LAUNCH_AGENT_LABEL = "com.xhs.assistant"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def generate_plist_dict() -> dict:
    """生成标准 LaunchAgent 字典结构。"""
    python_bin = sys.executable
    stdout_log = str(config.DATA_DIR / "launchagent.stdout.log")
    stderr_log = str(config.DATA_DIR / "launchagent.stderr.log")

    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            python_bin,
            "-m",
            "app.cli",
            "_serve",
        ],
        "WorkingDirectory": str(config.BASE_DIR),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
            "XHS_DATA_DIR": str(config.DATA_DIR),
        },
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,
            "Crashed": True,
        },
        "StandardOutPath": stdout_log,
        "StandardErrorPath": stderr_log,
    }


def generate_plist_xml() -> str:
    """输出格式化的 plist XML 文本。"""
    d = generate_plist_dict()
    return plistlib.dumps(d).decode("utf-8")


def install_service() -> dict:
    """安装并启动当前用户 LaunchAgent 服务。"""
    if sys.platform != "darwin":
        return {"ok": False, "message": "LaunchAgent 仅支持 macOS 系统"}

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    xml_content = generate_plist_xml()
    PLIST_PATH.write_text(xml_content, encoding="utf-8")
    os.chmod(PLIST_PATH, 0o644)

    # 尝试加载
    try:
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
            check=False,
        )
        res = subprocess.run(
            ["launchctl", "load", "-w", str(PLIST_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return {
                "ok": False,
                "message": f"launchctl load 失败: {res.stderr.strip()}",
                "plist_path": str(PLIST_PATH),
            }
    except Exception as e:
        return {"ok": False, "message": f"执行 launchctl 异常: {e}"}

    return {
        "ok": True,
        "message": "LaunchAgent 用户级常驻服务已成功安装并启动",
        "plist_path": str(PLIST_PATH),
    }


def uninstall_service() -> dict:
    """卸载并注销 LaunchAgent 服务。"""
    if sys.platform != "darwin":
        return {"ok": False, "message": "LaunchAgent 仅支持 macOS 系统"}

    if PLIST_PATH.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", "-w", str(PLIST_PATH)],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass
        PLIST_PATH.unlink(missing_ok=True)

    return {
        "ok": True,
        "message": "LaunchAgent 服务已注销并删除配置文件",
    }


def get_service_status() -> dict:
    """检查 LaunchAgent 安装与运行状态。"""
    if sys.platform != "darwin":
        return {
            "supported": False,
            "installed": False,
            "running": False,
            "platform": sys.platform,
        }

    installed = PLIST_PATH.exists()
    running = False
    pid = None

    if installed:
        try:
            res = subprocess.run(
                ["launchctl", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in res.stdout.splitlines():
                if LAUNCH_AGENT_LABEL in line:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        pid = int(parts[0])
                        running = True
                    else:
                        running = True  # 已注册但处于等待/休眠
                    break
        except Exception:
            pass

    return {
        "supported": True,
        "installed": installed,
        "running": running,
        "pid": pid,
        "plist_path": str(PLIST_PATH) if installed else None,
    }
