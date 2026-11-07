"""账号级浏览器互斥锁:防止 API 触发的登录/检测与后台 worker 抢同一 profile。

Chromium persistent context 对同一 user_data_dir 有 SingletonLock:
第二个实例启动会失败。API 线程的 check/login 与 worker 线程的发布/采集
必须按账号互斥(F-06.1:抢锁失败会被误判为"登录失效")。

本模块结合两级互斥防护:
1. 进程内 threading.Lock: 串行化同一进程内的线程与协程并发
2. 跨进程文件锁(fcntl.flock): 防止多个后台实例或外部调试进程争抢同一 Profile
"""
from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path

from .. import config

_thread_locks: dict[int, threading.Lock] = {}
_file_fds: dict[int, int] = {}
_registry_lock = threading.Lock()


def get_thread_lock(account_id: int) -> threading.Lock:
    with _registry_lock:
        if account_id not in _thread_locks:
            _thread_locks[account_id] = threading.Lock()
        return _thread_locks[account_id]


def _get_lock_file_path(account_id: int) -> Path:
    p_dir = config.PROFILES_DIR / f"account_{account_id}"
    p_dir.mkdir(parents=True, exist_ok=True)
    return p_dir / ".profile.lock"


def _acquire_file_lock(account_id: int) -> bool:
    """尝试获取跨进程文件锁 (UNIX/macOS 使用 fcntl)。"""
    if sys.platform == "win32":
        # Windows 回退: 依赖线程锁及 Chromium 原生 SingletonLock 保护
        return True

    import fcntl

    lock_file = _get_lock_file_path(account_id)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _registry_lock:
            # 记录当前持有的 fd
            _file_fds[account_id] = fd
        return True
    except (BlockingIOError, PermissionError, OSError):
        try:
            os.close(fd)
        except Exception:
            pass
        return False


def _release_file_lock(account_id: int) -> None:
    if sys.platform == "win32":
        return

    import fcntl

    with _registry_lock:
        fd = _file_fds.pop(account_id, None)

    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


def acquire(account_id: int, timeout: float = 0) -> bool:
    """尝试拿账号锁; 默认非阻塞(timeout=0), 拿不到说明该账号浏览器正被占用。"""
    t_lock = get_thread_lock(account_id)
    deadline = time.monotonic() + timeout

    while True:
        if t_lock.acquire(blocking=False):
            if _acquire_file_lock(account_id):
                return True
            # 获取文件锁失败(其他进程正在使用), 释放线程锁
            t_lock.release()

        if time.monotonic() >= deadline:
            return False

        time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))


def release(account_id: int) -> None:
    """释放账号锁(先释放跨进程文件锁, 再释放进程内线程锁)。"""
    try:
        _release_file_lock(account_id)
    finally:
        t_lock = get_thread_lock(account_id)
        if t_lock.locked():
            try:
                t_lock.release()
            except RuntimeError:
                pass
