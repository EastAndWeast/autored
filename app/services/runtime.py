"""运行状态与诊断服务。"""
from __future__ import annotations

from .. import config
from ..models import Account, PublishJob, SessionLocal


def get_runtime_status_service() -> dict:
    with SessionLocal() as s:
        total_accounts = s.query(Account).count()
        online_accounts = s.query(Account).filter(Account.status == "online").count()
        active_jobs = (
            s.query(PublishJob)
            .filter(PublishJob.status.in_(("queued", "publishing", "submitted", "verifying")))
            .count()
        )

    return {
        "version": "0.2.0",
        "name": "小红书 AI 运营助手",
        "service_url": f"http://{config.HOST}:{config.PORT}/",
        "data_dir": str(config.DATA_DIR),
        "timezone": config.DEFAULT_TIMEZONE,
        "accounts": {
            "total": total_accounts,
            "online": online_accounts,
        },
        "active_jobs_count": active_jobs,
        "features": {
            "draft_isolation": True,
            "idempotency_protection": True,
            "crash_recovery": True,
            "mcp_stdio_ready": True,
        },
    }

