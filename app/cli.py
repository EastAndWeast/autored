"""小红书 AI 运营助手 - 核心服务管理与运维命令行 (CLI)。

支持命令:
- start [--daemon]: 启动本地常驻服务(单实例互斥,防重复启动与双 worker)
- stop: 安全优雅终止正在运行的后台服务
- status: 检查当前服务健康状态、PID、端口、在线账号与活跃任务
- doctor: 全面环境诊断(Python环境、依赖、权限、数据库迁移、浏览器、Profile锁)
- open: 在默认浏览器中打开管理网页
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config
from .migrations.runner import get_applied_versions
from .models import Account, Note, PublishJob, SessionLocal, init_db

PID_FILE = config.DATA_DIR / "xhs_assistant.pid"
LOG_FILE = config.DATA_DIR / "server.log"

# Windows 没有 SIGKILL(访问即 AttributeError);os.kill 其他信号值在 Windows
# 走 TerminateProcess 无条件终止,语义上等同强杀
HARD_KILL_SIG = getattr(signal, "SIGKILL", signal.SIGTERM)


# Windows 控制台默认 charmap(cp1252/gbk),打印 emoji 会 UnicodeEncodeError
# 在 CLI 入口统一把标准流重配为 UTF-8(其他平台无副作用)
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def get_running_pid() -> int | None:
    """获取当前正在运行的服务进程 PID;若进程已死则清理残留 pid 文件。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return None

    try:
        # 向进程发送 0 信号探测其存活状态 (Unix/macOS)
        os.kill(pid, 0)
        return pid
    except (OSError, ProcessLookupError):
        PID_FILE.unlink(missing_ok=True)
        return None


def is_port_in_use(port: int = config.PORT, host: str = config.HOST) -> bool:
    """检查指定端口是否处于监听状态。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def check_health(timeout: float = 1.0) -> dict | None:
    """尝试请求本地 health 检查端点。"""
    url = f"http://{config.HOST}:{config.PORT}/api/health"
    try:
        req = urllib.request.Request(url, headers={"Host": f"{config.HOST}:{config.PORT}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                import json
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def cmd_start(daemon: bool = False) -> int:
    """启动本地服务。"""
    pid = get_running_pid()
    if pid is not None:
        print(f"\n[提示] 小红书 AI 运营助手已在运行 (PID: {pid})")
        print(f"      管理端地址: http://{config.HOST}:{config.PORT}/\n")
        return 0

    if is_port_in_use():
        health = check_health()
        if health:
            print(f"\n[提示] 端口 {config.PORT} 已有小红书运营助手运行中")
            print(f"      管理端地址: http://{config.HOST}:{config.PORT}/\n")
            return 0
        print(f"\n[错误] 端口 {config.PORT} 已被其他程序占用,请先排查占用程序或修改 app/config.py\n")
        return 1

    if daemon:
        # 后台守护进程模式启动
        print(f"\n正在以后台模式启动小红书 AI 运营助手...")
        cmd = [sys.executable, "-m", "app.cli", "_serve"]
        log_f = open(LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            cwd=str(config.BASE_DIR),
            close_fds=True,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        # 等待端口就绪
        ready = False
        for _ in range(30):
            time.sleep(0.2)
            if check_health(timeout=0.5):
                ready = True
                break

        if ready:
            print(f"✅ 服务已在后台成功启动 (PID: {proc.pid})")
            print(f"   访问地址: http://{config.HOST}:{config.PORT}/")
            print(f"   运行日志: {LOG_FILE}\n")
            return 0
        else:
            print(f"⚠️ 服务已启动但健康检查暂未响应,请检查日志: {LOG_FILE}\n")
            return 0
    else:
        # 前台直接运行
        _run_server_foreground()
        return 0


def _run_server_foreground() -> None:
    """前台运行服务,主进程写入 PID并在退出时清理。"""
    import uvicorn

    my_pid = os.getpid()
    PID_FILE.write_text(str(my_pid), encoding="utf-8")

    def _cleanup(*args):
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    print(f"\n  🚀 小红书 AI 运营助手 (PID: {my_pid}) 启动中...")
    print(f"  🌐 管理端地址 → http://{config.HOST}:{config.PORT}/\n")

    try:
        uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="warning")
    finally:
        PID_FILE.unlink(missing_ok=True)


def cmd_stop() -> int:
    """停止后台运行中的服务。"""
    pid = get_running_pid()
    if pid is None:
        if is_port_in_use():
            print(f"\n[提示] 未发现 PID 记录,但端口 {config.PORT} 仍有进程占用。")
        else:
            print(f"\n[提示] 小红书 AI 运营助手未运行。\n")
        return 0

    print(f"正在停止服务 (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            print(f"进程 {pid} 未按时退出,正在强制终止...")
            try:
                os.kill(pid, HARD_KILL_SIG)
            except OSError:
                pass
    except Exception as e:
        print(f"停止进程异常: {e}")

    PID_FILE.unlink(missing_ok=True)
    print("✅ 服务已成功停止。\n")
    return 0


def cmd_status() -> int:
    """输出当前运行状态。"""
    pid = get_running_pid()
    port_used = is_port_in_use()
    health = check_health()

    print("\n================ 小红书 AI 运营助手状态 ================")
    if pid or port_used or health:
        print(f"  运行状态: 🟢 运行中")
        if pid:
            print(f"  进程 PID: {pid}")
        print(f"  服务端口: {config.PORT} ({'正常监听' if port_used else '未监听'})")
        print(f"  网页地址: http://{config.HOST}:{config.PORT}/")
        if health:
            print(f"  核心版本: {health.get('version', '未知')}")
    else:
        print(f"  运行状态: 🔴 未运行")
        print(f"  提示: 可使用 'python run.py start' 启动服务")

    print(f"  数据目录: {config.DATA_DIR}")
    print(f"  数据库路径: {config.DB_PATH}")

    # 查阅数据库概况
    try:
        init_db()
        with SessionLocal() as s:
            total_acc = s.query(Account).count()
            online_acc = s.query(Account).filter(Account.status == "online").count()
            queued_jobs = s.query(PublishJob).filter(PublishJob.status.in_(("queued", "publishing"))).count()
            scheduled_jobs = s.query(PublishJob).filter(PublishJob.status == "scheduled").count()
            overdue_jobs = s.query(PublishJob).filter(PublishJob.status == "overdue").count()
            verifying_jobs = s.query(PublishJob).filter(PublishJob.status == "verifying").count()
            total_notes = s.query(Note).count()

        print(f"  账号统计: 总计 {total_acc} 个 (在线: {online_acc} 个)")
        print(f"  笔记存量: {total_notes} 篇")
        print(f"  任务队列: 排队/发布中 {queued_jobs} 个 | 定时排期 {scheduled_jobs} 个")
        if overdue_jobs > 0:
            print(f"  ⚠️ 逾期任务: {overdue_jobs} 个 (需人工在网页核对/重排)")
        if verifying_jobs > 0:
            print(f"  ⚠️ 待核对任务: {verifying_jobs} 个 (需平台对账确认,禁止盲目重发)")
    except Exception as e:
        print(f"  数据库查询: 异常 ({e})")

    print("========================================================\n")
    return 0


def cmd_doctor() -> int:
    """全面环境诊断检查。"""
    print("\n🔍 正在运行环境健康体检 (Doctor)...\n")
    issues = 0

    # 1. Python 环境
    py_ver = sys.version.split()[0]
    print(f"  [1/7] Python 环境: {sys.executable} (版本: {py_ver})", end=" ")
    if sys.version_info < (3, 9):
        print("❌ 要求 Python 3.9 及以上")
        issues += 1
    else:
        print("✅ 正常")

    # 2. 数据目录与权限
    print(f"  [2/7] 数据存储目录: {config.DATA_DIR}", end=" ")
    try:
        for d in (config.DATA_DIR, config.UPLOADS_DIR, config.PROFILES_DIR):
            d.mkdir(parents=True, exist_ok=True)
            test_f = d / ".doctor_test_write"
            test_f.write_text("ok", encoding="utf-8")
            test_f.unlink()
        print("✅ 读写权限正常")
    except Exception as e:
        print(f"❌ 目录权限不可写: {e}")
        issues += 1

    # 3. 数据库与迁移状态
    print(f"  [3/7] 数据库迁移状态:", end=" ")
    try:
        init_db()
        applied = get_applied_versions(config.DB_PATH)
        print(f"✅ 已应用迁移版本: {applied}")
    except Exception as e:
        print(f"❌ 数据库异常: {e}")
        issues += 1

    # 4. 浏览器组件与 Patchright
    print(f"  [4/7] 自动化反检测浏览器引擎:", end=" ")
    try:
        import patchright
        print("✅ patchright 模块可用")
    except ImportError:
        print("❌ 未安装 patchright 依赖,请运行 pip install patchright")
        issues += 1

    # 5. 账号 Profile 互斥锁检测
    print(f"  [5/7] 账号 Profile 互斥与死锁检测:", end=" ")
    try:
        from .xhs.profile_lock import _get_lock_file_path
        with SessionLocal() as s:
            acc_list = s.query(Account).all()
        locked_accs = []
        for a in acc_list:
            lf = _get_lock_file_path(a.id)
            if lf.exists():
                locked_accs.append(a.id)
        if locked_accs:
            print(f"ℹ️ 发现 {len(locked_accs)} 个账号存在锁文件记录 (可能正有任务运行或上次异常残留)")
        else:
            print("✅ 所有账号锁均处于空闲就绪状态")
    except Exception as e:
        print(f"⚠️ 锁检测提示: {e}")

    # 6. 任务积压与逾期检查
    print(f"  [6/7] 任务健康与逾期检查:", end=" ")
    try:
        with SessionLocal() as s:
            verifying = s.query(PublishJob).filter(PublishJob.status == "verifying").count()
            overdue = s.query(PublishJob).filter(PublishJob.status == "overdue").count()
        if verifying > 0 or overdue > 0:
            print(f"⚠️ 注意: 存在 {overdue} 个逾期计划与 {verifying} 个待平台核对任务")
        else:
            print("✅ 无异常积压任务")
    except Exception as e:
        print(f"⚠️ 任务统计异常: {e}")

    # 7. 端口占用
    print(f"  [7/7] 本地通信端口 ({config.PORT}):", end=" ")
    if is_port_in_use():
        h = check_health()
        if h:
            print(f"✅ 已被当前小红书运营助手服务正常占用 (PID: {get_running_pid() or '未知'})")
        else:
            print(f"❌ 被未知非本服务程序占用,可能冲突")
            issues += 1
    else:
        print("✅ 端口空闲就绪")

    print("\n--------------------------------------------------------")
    if issues == 0:
        print("🎉 诊断完成! 未发现阻碍运行的关键异常,系统处于健康状态。")
    else:
        print(f"⚠️ 诊断发现 {issues} 处异常,请参考上方提示进行修复。")
    print("--------------------------------------------------------\n")
    return 0 if issues == 0 else 1


def cmd_open() -> int:
    """在系统默认浏览器中打开管理端网页。"""
    url = f"http://{config.HOST}:{config.PORT}/"
    print(f"正在浏览器中打开: {url}")
    webbrowser.open(url)
    return 0


def cmd_service(subaction: str = "status") -> int:
    """macOS LaunchAgent 用户级服务管理。"""
    from .services.launchagent import get_service_status, install_service, uninstall_service

    if subaction == "install":
        res = install_service()
        print(f"[{'成功' if res['ok'] else '失败'}] {res['message']}")
        return 0 if res["ok"] else 1
    elif subaction == "uninstall":
        res = uninstall_service()
        print(f"[{'成功' if res['ok'] else '失败'}] {res['message']}")
        return 0
    else:  # status
        st = get_service_status()
        print("\n==== macOS LaunchAgent 开机/登录常驻服务状态 ====")
        print(f"  支持平台: {'macOS (Darwin)' if st['supported'] else '不支持 (非 macOS)'}")
        print(f"  安装状态: {'已安装配置' if st['installed'] else '未安装'}")
        print(f"  运行状态: {'🟢 运行中 (PID: ' + str(st['pid']) + ')' if st['running'] else '🔴 未运行'}")
        if st.get("plist_path"):
            print(f"  配置路径: {st['plist_path']}")
        print("================================================\n")
        return 0


def cmd_backup(subaction: str = "create", target: str = "") -> int:
    """数据一致性备份与恢复。"""
    from .services.backup import create_backup, list_backups, restore_backup

    if subaction == "create":
        res = create_backup(tag=target)
        print(f"\n✅ 数据全量快照备份成功: {res['folder_name']}")
        print(f"   存储路径: {res['backup_path']}")
        print(f"   文件数量: {res['manifest']['total_files']} 个 | Schema版本: {res['manifest']['max_schema']}\n")
        return 0
    elif subaction == "list":
        items = list_backups()
        print(f"\n==== 数据备份快照列表 (共 {len(items)} 个) ====")
        for it in items:
            print(f"  📦 {it['folder']}  (时间: {it.get('created_at','?')}, 文件数: {it.get('total_files','?')})")
        print("================================================\n")
        return 0
    elif subaction == "restore":
        if not target:
            print("[错误] 请指定要恢复的备份文件夹名称或路径 (例如: python run.py backup restore snapshot_20261106_120000)")
            return 1
        res = restore_backup(target)
        print(f"\n✅ 数据已安全恢复自: {res['restored_from']}")
        print(f"   恢复前安全快照已自动保存: {res['pre_restore_backup_tag']}\n")
        return 0
    return 0


def cmd_configure(host: str = "codex") -> int:
    """宿主 AI (Codex / WorkBuddy) 接入配置安全合并。"""
    from .services.host_config import configure_codex, configure_workbuddy

    if host.lower() == "codex":
        res = configure_codex()
        print(f"\n✅ {res['message']}")
        print(f"   配置文件路径: {res['config_file']}")
        if res.get("backup_created"):
            print("   原配置文件已备份为 .json.bak")
        print(f"   当前生效的 MCP 服务列表: {', '.join(res['all_mcp_servers'])}\n")
        return 0
    elif host.lower() == "workbuddy":
        res = configure_workbuddy()
        print(f"\n✅ {res['message']}")
        print(f"   连接器包路径: {res['connector_dir']}\n")
        return 0
    else:
        print(f"[错误] 不支持的宿主类型: {host}, 可选为 codex 或 workbuddy")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书 AI 运营助手 - 服务管理与运维 CLI")
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "stop", "status", "doctor", "open", "service", "backup", "configure", "_serve"],
        help="执行操作: start(启动), stop(停止), status(状态), doctor(诊断), open(打开网页), service(开机常驻服务), backup(备份管理), configure(宿主接入配置)",
    )
    parser.add_argument("subaction", nargs="?", default="", help="子命令 (例如 service install, backup create, configure codex)")
    parser.add_argument("extra", nargs="?", default="", help="附加参数 (如备份标识或恢复路径)")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台运行模式(配合 start 使用)")

    args = parser.parse_args()

    if args.action == "start":
        return cmd_start(daemon=args.daemon)
    elif args.action == "_serve":
        _run_server_foreground()
        return 0
    elif args.action == "stop":
        return cmd_stop()
    elif args.action == "status":
        return cmd_status()
    elif args.action == "doctor":
        return cmd_doctor()
    elif args.action == "open":
        return cmd_open()
    elif args.action == "service":
        return cmd_service(subaction=args.subaction or "status")
    elif args.action == "backup":
        return cmd_backup(subaction=args.subaction or "create", target=args.extra)
    elif args.action == "configure":
        return cmd_configure(host=args.subaction or "codex")
    return 0


if __name__ == "__main__":
    sys.exit(main())
