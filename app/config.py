"""全局配置:路径、端口、防封频控参数。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def resolve_default_data_dir() -> Path:
    """根据操作系统规范与便携模式解析用户数据目录。"""
    import sys
    env_dir = os.environ.get("XHS_DATA_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    portable_data = BASE_DIR / "data"
    if (portable_data / "app.db").exists():
        return portable_data.resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "xhs-assistant").resolve()
    elif sys.platform == "win32":
        app_data = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (Path(app_data) / "xhs-assistant").resolve()
    else:
        return (Path.home() / ".local" / "share" / "xhs-assistant").resolve()


DATA_DIR = resolve_default_data_dir()
UPLOADS_DIR = DATA_DIR / "uploads"
PROFILES_DIR = DATA_DIR / "profiles"
DB_PATH = DATA_DIR / "app.db"
WEB_DIR = BASE_DIR / "app" / "web"
VENDOR_DIR = BASE_DIR / "app" / "vendor"

HOST = "127.0.0.1"
PORT = int(os.environ.get("XHS_PORT", "8643"))
DEFAULT_TIMEZONE = os.environ.get("XHS_TIMEZONE", "Asia/Shanghai")


def _ensure_dir(d: Path, retries: int = 3) -> None:
    """mkdir(exist_ok=True) + Windows 竞争重试。

    Windows CI/杀软实时扫描下,紧邻的两次 mkdir 偶发 WinError 183
    (目录已存在但 exist_ok 的 is_dir/stat 瞬时失败),重试即可吸收。
    """
    import time

    last_err: Exception | None = None
    for i in range(retries):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError as e:
            if d.is_dir():
                return
            last_err = e
        except OSError as e:  # WinError 共享冲突等瞬时错误
            last_err = e
        time.sleep(0.2 * (i + 1))
    raise last_err if last_err else OSError(f"mkdir failed: {d}")


def configure_data_dir(data_dir: Path | str) -> None:
    """动态重置数据目录(测试或用户配置路径切换)。"""
    global DATA_DIR, UPLOADS_DIR, PROFILES_DIR, DB_PATH
    DATA_DIR = Path(data_dir).resolve()
    UPLOADS_DIR = DATA_DIR / "uploads"
    PROFILES_DIR = DATA_DIR / "profiles"
    DB_PATH = DATA_DIR / "app.db"
    for _d in (DATA_DIR, UPLOADS_DIR, PROFILES_DIR):
        _ensure_dir(_d)


for _d in (DATA_DIR, UPLOADS_DIR, PROFILES_DIR):
    _ensure_dir(_d)

# ---- 防封频控(可在设置页调整) ----
DEFAULT_DAILY_LIMIT = 2              # 每账号每日发布上限
INTER_ACCOUNT_DELAY = (300, 900)     # 相邻发布任务之间的随机间隔(秒),5~15 分钟
SCHEDULE_JITTER = (0, 600)           # 本地定时发布的随机抖动(秒),0~10 分钟
PLATFORM_SCHEDULE_THRESHOLD_H = 2.5  # 距今超过该小时数走平台原生定时发布
OVERDUE_THRESHOLD_MINUTES = 60       # 本地定时计划超过 60 分钟未执行视为逾期(防电脑休眠开机后盲目补发)
UPLOAD_POLL_INTERVAL = 2             # 视频上传进度轮询间隔(秒)
VIDEO_UPLOAD_TIMEOUT = 15 * 60       # 视频上传超时
LOGIN_TIMEOUT = 600                  # 扫码登录等待超时(秒)
DEFAULT_COLLECT_TIMES = ["10:30", "21:30"]  # 每日数据采集时间点(会加随机分钟)

# 数据采集(xhs-toolkit 源码实测经验)
STATS_PAGE_SETTLE = 6        # 页面加载后等待秒数
STATS_TABLE_WAIT = 20        # 数据表格出现的最长等待秒数
STATS_URL_CANDIDATES = [     # 创作者中心「内容分析」页(多入口兜底)
    "/statistics/data-analysis",   # xhs-toolkit 实测主入口
    "/analytics/note",             # 旧入口兜底
]

# 发布页
CREATOR_BASE = "https://creator.xiaohongshu.com"
PUBLISH_URL_IMAGE = CREATOR_BASE + "/publish/publish?from=homepage&target=image"
PUBLISH_URL_VIDEO = CREATOR_BASE + "/publish/publish?from=homepage&target=video"
LOGIN_URL = CREATOR_BASE + "/login"
PUBLISH_SUCCESS_PATTERN = "**/publish/success?**"

# 浏览器指纹池:账号创建时随机取一个并永久固定
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
VIEWPORT_POOL = [(1440, 900), (1512, 982), (1536, 864), (1920, 1080)]
