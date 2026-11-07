"""v1 发布任务 API:负责排期、任务详情、状态轮询与任务取消。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...models import PublishAttempt, PublishJob, SessionLocal
from ...services.errors import ServiceError
from ...services.publish import cancel_publish_job, schedule_publish

router = APIRouter(tags=["v1-publish-jobs"])


def _job_dict(job: PublishJob, s=None) -> dict:
    attempts_data = []
    if s:
        attempts = (
            s.query(PublishAttempt)
            .filter(PublishAttempt.job_id == job.id)
            .order_by(PublishAttempt.id.asc())
            .all()
        )
        attempts_data = [
            {
                "attempt_no": att.attempt_no,
                "stage": att.stage,
                "status": att.status,
                "started_at": att.started_at.isoformat(sep=" "),
                "ended_at": att.ended_at.isoformat(sep=" ") if att.ended_at else None,
                "platform_note_id": att.platform_note_id,
                "detail_url": att.detail_url,
                "error_code": att.error_code,
                "error_message": att.error_message,
            }
            for att in attempts
        ]

    return {
        "id": job.id,
        "note_id": job.note_id,
        "account_id": job.account_id,
        "note_revision": job.note_revision,
        "mode": job.mode,
        "scheduled_at": job.scheduled_at.isoformat(sep=" ") if job.scheduled_at else None,
        "effective_at": job.effective_at.isoformat(sep=" ") if job.effective_at else None,
        "timezone": job.timezone,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "cancel_requested": job.cancel_requested,
        "last_error_code": job.last_error_code,
        "last_error_message": job.last_error_message,
        "content_snapshot": job.content_snapshot,
        "attempts": attempts_data,
        "created_at": job.created_at.isoformat(sep=" "),
        "updated_at": job.updated_at.isoformat(sep=" "),
    }


class SchedulePublishIn(BaseModel):
    mode: str = "immediate"  # immediate | schedule
    scheduled_at: str | None = None
    timezone: str = "Asia/Shanghai"
    idempotency_key: str | None = None
    expected_revision: int | None = None


@router.post("/notes/{note_id}/publish-jobs")
def post_publish_job(note_id: int, body: SchedulePublishIn):
    try:
        job = schedule_publish(
            note_id=note_id,
            mode=body.mode,
            scheduled_at=body.scheduled_at,
            timezone=body.timezone,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        )
        with SessionLocal() as s:
            return _job_dict(job, s)
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})
    except ValueError as e:
        raise HTTPException(400, {"code": "VALIDATION_ERROR", "message": str(e)})


@router.get("/publish-jobs/{job_id}")
def get_publish_job(job_id: int):
    with SessionLocal() as s:
        job = s.get(PublishJob, job_id)
        if not job:
            raise HTTPException(404, {"code": "JOB_NOT_FOUND", "message": f"任务 #{job_id} 不存在"})
        return _job_dict(job, s)


@router.post("/publish-jobs/{job_id}/cancel")
def post_cancel_publish_job(job_id: int):
    try:
        job = cancel_publish_job(job_id)
        with SessionLocal() as s:
            return _job_dict(job, s)
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})

