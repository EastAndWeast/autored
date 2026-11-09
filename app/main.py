"""FastAPI 入口:路由注册 + 静态界面 + 调度启动。"""
from __future__ import annotations

from pathlib import Path
import hashlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api import accounts, notes, settings, stats
from .api.v1 import router as v1_router
from .models import LogEntry, SessionLocal, init_db
from .scheduler import shutdown as sched_shutdown
from .scheduler import start as sched_start

app = FastAPI(title="小红书发布助手", docs_url=None, redoc_url=None)

# ---- S-02: 本地服务的 CSRF / DNS-Rebinding 防护 ----
# 只接受本机 Host;写请求的 Origin 必须是本机页面(浏览器跨站请求会带对方 Origin,
# 恶意网页的 no-cors/表单请求因此被 403 拦截;curl 等无 Origin 的本机调用不受影响)
_ALLOWED_HOSTS = {f"{config.HOST}:{config.PORT}", f"localhost:{config.PORT}"}
_ALLOWED_ORIGINS = {f"http://{config.HOST}:{config.PORT}", f"http://localhost:{config.PORT}"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    host_full = request.headers.get("host") or ""
    if host_full not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"detail": f"forbidden origin: {origin}"}, status_code=403)
    return await call_next(request)

app.include_router(accounts.router)
app.include_router(notes.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(v1_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _cleanup_state_files()
    _tighten_permissions()
    sched_start()


def _cleanup_state_files() -> None:
    """删除历史遗留的明文会话导出(persistent context 已含全部状态,S-03)。"""
    import os

    for profile in config.PROFILES_DIR.glob("account_*"):
        f = profile / "state.json"
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass


def _tighten_permissions() -> None:
    """数据目录与库文件仅限当前用户读写(S-03)。

    Windows 的 chmod 是粗粒度位映射(0600 会呈现为 0o666),语义无效,直接跳过。
    """
    import os
    import sys

    if sys.platform == "win32":
        return

    for d in (config.DATA_DIR, config.PROFILES_DIR, config.UPLOADS_DIR):
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass
    try:
        os.chmod(config.DB_PATH, 0o600)
    except Exception:
        pass
    for profile in config.PROFILES_DIR.glob("account_*"):
        try:
            os.chmod(profile, 0o700)
        except Exception:
            pass


@app.on_event("shutdown")
def _shutdown() -> None:
    sched_shutdown()


# ---- 日志 API ----
from fastapi import Query  # noqa: E402


@app.get("/api/logs")
def get_logs(limit: int = Query(200, le=1000)):
    with SessionLocal() as s:
        rows = s.query(LogEntry).order_by(LogEntry.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "level": r.level,
                "source": r.source,
                "message": r.message,
                "created_at": r.created_at.isoformat(sep=" "),
            }
            for r in rows
        ]


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.1.0"}


# ---- 静态界面 ----
app.mount("/static/uploads", StaticFiles(directory=str(config.UPLOADS_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR / "static")), name="static")


@app.get("/")
def index():
    # 模板与脚本必须同步更新，避免旧缓存缺少新模板引用的函数。
    script = config.WEB_DIR / "static" / "app.js"
    version = hashlib.sha256(script.read_bytes()).hexdigest()[:16]
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={version}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/vendor/{fname}")
def vendor(fname: str):
    path = config.VENDOR_DIR / fname
    if not path.exists() or "/" in fname or ".." in fname:
        raise HTTPException(404)
    return FileResponse(str(path))
