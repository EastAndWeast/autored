"""账号领域服务:负责账号查询、创建、登录状态维护。"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from .. import config
from ..models import Account, Note, SessionLocal, log
from ..xhs.browser import generate_fingerprint
from ..xhs.login import get_login_state, start_login
from .errors import AccountNotFoundError, ServiceError


def account_to_dict(a: Account, session: Session | None = None) -> dict:
    today = datetime.combine(datetime.now().date(), datetime.min.time())
    def _calc(s: Session) -> int:
        return (
            s.query(Note)
            .filter(
                Note.account_id == a.id,
                Note.status == "published",
                Note.published_at >= today,
            )
            .count()
        )

    published_today = _calc(session) if session else 0

    return {
        "id": a.id,
        "name": a.name,
        "platform_nickname": a.platform_nickname or a.name,
        "platform_region": a.platform_region or "unknown",
        "creator_base": a.creator_base or ("https://creator.rednote.com" if a.platform_region == "global" else "https://creator.xiaohongshu.com"),
        "status": a.status,
        "timezone": a.timezone or "Asia/Shanghai",
        "daily_limit": a.daily_limit,
        "published_today": published_today,
        "last_check_at": a.last_check_at.isoformat(sep=" ") if a.last_check_at else None,
        "last_publish_at": a.last_publish_at.isoformat(sep=" ") if a.last_publish_at else None,
    }


def list_accounts_service(status: str | None = None) -> list[dict]:
    with SessionLocal() as s:
        q = s.query(Account).order_by(Account.id.asc())
        if status and status != "all":
            q = q.filter(Account.status == status)
        accounts = q.all()
        return [account_to_dict(a, s) for a in accounts]


def get_account_service(account_id: int) -> dict:
    with SessionLocal() as s:
        a = s.get(Account, account_id)
        if not a:
            raise AccountNotFoundError(f"账号 #{account_id} 不存在")
        return account_to_dict(a, s)


def trigger_login_service(name: str | None = None, account_id: int | None = None) -> dict:
    """发起扫码登录(新建或重新扫码)。"""
    with SessionLocal() as s:
        if account_id:
            acc = s.get(Account, account_id)
            if not acc:
                raise AccountNotFoundError(f"账号 #{account_id} 不存在")
            target_id = acc.id
            account_name = acc.name
        else:
            if not name or not name.strip():
                raise ServiceError("INVALID_NAME", "新建账号必须提供备注名", 400)
            clean_name = name.strip()
            exists = s.query(Account).filter(Account.name == clean_name).first()
            if exists:
                target_id = exists.id
                account_name = exists.name
            else:
                ua, w, h = generate_fingerprint()
                acc = Account(name=clean_name, ua=ua, viewport_w=w, viewport_h=h, status="pending")
                s.add(acc)
                s.commit()
                target_id = acc.id
                account_name = acc.name

    started = start_login(target_id)
    web_url = f"http://{config.HOST}:{config.PORT}/"

    return {
        "account_id": target_id,
        "account_name": account_name,
        "started": started,
        "web_url": web_url,
        "message": f"已为账号[{account_name}]开启扫码登录流程。可在浏览器打开 {web_url} 查看二维码并用小红书 APP 扫码确认。",
    }


def get_login_status_service(account_id: int) -> dict:
    """获取当前登录任务与二维码状态。"""
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise AccountNotFoundError(f"账号 #{account_id} 不存在")
        acc_status = acc.status

    st = get_login_state(account_id)
    if not st:
        return {
            "account_id": account_id,
            "status": acc_status,
            "message": "当前没有正在进行的扫码任务" if acc_status == "online" else "待扫码",
            "has_qr": False,
        }

    return {
        "account_id": account_id,
        "status": st.get("status", acc_status),
        "message": st.get("message", ""),
        "has_qr": bool(st.get("qr")),
    }

