"""账号管理 API:增删、扫码登录、状态检测。"""
from __future__ import annotations

import re
import shutil
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models import Account, Note, SessionLocal, log
from .. import config
from ..xhs.browser import generate_fingerprint
from ..xhs.login import check_cookie, get_login_state, start_login
from ..xhs import profile_lock
from ..scheduler import enqueue

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def mask_proxy(proxy: str | None) -> str:
    """脱敏代理凭据:http://user:pass@host:port -> http://user:***@host:port"""
    if not proxy:
        return ""
    return re.sub(r"(//[^/@:]+:)[^@]+(@)", r"\1***\2", proxy)


def _account_dict(s, a: Account) -> dict:
    today = datetime.combine(datetime.now().date(), datetime.min.time())
    published_today = (
        s.query(Note)
        .filter(
            Note.account_id == a.id,
            Note.status == "published",
            Note.published_at >= today,
        )
        .count()
    )
    return {
        "id": a.id,
        "name": a.name,
        "status": a.status,
        "proxy": mask_proxy(a.proxy),
        "daily_limit": a.daily_limit,
        "published_today": published_today,
        "last_check_at": a.last_check_at.isoformat(sep=" ") if a.last_check_at else None,
        "last_publish_at": a.last_publish_at.isoformat(sep=" ") if a.last_publish_at else None,
    }


@router.get("")
def list_accounts():
    with SessionLocal() as s:
        return [_account_dict(s, a) for a in s.query(Account).order_by(Account.id).all()]


class AccountIn(BaseModel):
    name: str


@router.post("")
async def create_account(body: AccountIn):
    """创建账号并启动扫码登录。"""
    with SessionLocal() as s:
        exists = s.query(Account).filter(Account.name == body.name).first()
        if exists:
            raise HTTPException(400, "已存在同名账号")
        ua, w, h = generate_fingerprint()
        acc = Account(name=body.name.strip(), ua=ua, viewport_w=w, viewport_h=h)
        s.add(acc)
        s.commit()
        acc_id = acc.id

    log("info", "account", f"新增账号[{body.name}],等待扫码登录")
    start_login(acc_id)
    return {"id": acc_id}


@router.get("/{account_id}/qr")
def account_qr(account_id: int):
    state = get_login_state(account_id)
    if not state:
        return {"status": "none", "qr": None, "message": ""}
    return {
        "status": state.get("status"),
        "qr": state.get("qr"),
        "message": state.get("message", ""),
    }


@router.post("/{account_id}/login")
async def relogin(account_id: int):
    """重新扫码(登录失效时)。"""
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
    if not profile_lock.acquire(account_id):
        raise HTTPException(409, "该账号正在执行发布/采集任务,请稍后再试")
    profile_lock.release(account_id)
    if not start_login(account_id):
        raise HTTPException(409, "该账号正在登录中")
    return {"ok": True}


@router.post("/{account_id}/check")
async def check_login(account_id: int):
    """立即校验登录态。账号正被发布/采集占用时返回 busy,不误判失效。"""
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
    if not profile_lock.acquire(account_id):
        return {"ok": None, "message": "该账号正在执行发布/采集任务,请稍后再检测"}
    try:
        ok = await check_cookie(acc)
    finally:
        profile_lock.release(account_id)
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if acc:
            acc.status = "online" if ok else "expired"
            acc.last_check_at = datetime.now()
            s.commit()
    return {"ok": ok}


class AccountPatch(BaseModel):
    name: str | None = None
    proxy: str | None = None
    daily_limit: int | None = None


@router.patch("/{account_id}")
def patch_account(account_id: int, body: AccountPatch):
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if body.name is not None and body.name.strip():
            acc.name = body.name.strip()
        if body.proxy is not None:
            new_proxy = body.proxy.strip()
            if "***" in new_proxy:
                pass  # 前端回传的是脱敏值,保持原凭据不变
            else:
                acc.proxy = new_proxy or None
        if body.daily_limit is not None:
            acc.daily_limit = max(1, min(10, body.daily_limit))
        s.commit()
        return _account_dict(s, acc)


@router.delete("/{account_id}")
def delete_account(account_id: int):
    from ..xhs.login import cancel_login

    cancel_login(account_id)  # 若扫码登录还在后台跑,先终止,防止其重建 profile
    if not profile_lock.acquire(account_id, timeout=30):
        raise HTTPException(409, "该账号浏览器正被占用(发布/采集进行中),请稍后再删除")
    try:
        _do_delete_account(account_id)
    finally:
        profile_lock.release(account_id)
    return {"ok": True}


def _do_delete_account(account_id: int) -> None:
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        name = acc.name
        media_files = []
        for note in acc.notes:
            media_files.extend(note.media_paths or [])
        s.delete(acc)  # ORM 级联删除 notes -> note_stats
        s.commit()
    # 清理磁盘:媒体文件与浏览器环境
    for rel in media_files:
        try:
            f = config.UPLOADS_DIR / rel
            if f.exists():
                f.unlink()
        except Exception:
            pass
    profile = config.PROFILES_DIR / f"account_{account_id}"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    log("info", "account", f"删除账号[{name}]及浏览器环境")


@router.post("/{account_id}/collect")
def collect_now(account_id: int):
    """手动触发该账号数据采集(入队,由 worker 串行执行)。"""
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if acc.status != "online":
            raise HTTPException(400, "账号不在线,请先登录")
    enqueue("collect", account_id)
    return {"ok": True}
