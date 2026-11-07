"""发布任务服务:负责可发布性校验、排期幂等、快照固化、任务取消与故障恢复。"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import config
from ..models import Account, Note, PublishAttempt, PublishJob, SessionLocal, log
from .errors import (
    AccountNotFoundError,
    AccountNotOnlineError,
    CannotCancelError,
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    NoteNotFoundError,
    PublishNotReadyError,
    RevisionConflictError,
)

ACTIVE_JOB_STATUSES = {
    "queued",
    "publishing",
    "submitted",
    "verifying",
    "platform_scheduled",
}


def validate_publish_ready(note: Note) -> tuple[bool, list[str]]:
    """发布要素完整性校验:发布时才做硬校验,草稿阶段不阻断。"""
    missing: list[str] = []
    if not (note.title or "").strip():
        missing.append("title")
    if not note.media_paths:
        missing.append("media_paths")
    return (len(missing) == 0, missing)


def apply_jitter(when: datetime) -> datetime:
    from datetime import timedelta
    return when + timedelta(seconds=random.randint(*config.SCHEDULE_JITTER))


def _compute_payload_hash(payload: dict) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def schedule_publish(
    note_id: int,
    mode: str = "immediate",
    scheduled_at: datetime | str | None = None,
    timezone: str = "Asia/Shanghai",
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    session: Session | None = None,
) -> PublishJob:
    """创建或获取持久发布任务:
    - 校验发布要素
    - 修订号乐观锁
    - 幂等去重(相同 key + 相同 payload 返回原任务,不同则报冲突)
    - 固化内容快照(content_snapshot),防止排队期间后续修改草稿发生内容错配
    """

    def _do(s: Session) -> PublishJob:
        note = s.get(Note, note_id)
        if not note:
            raise NoteNotFoundError(f"笔记 #{note_id} 不存在")

        acc = s.get(Account, note.account_id)
        if not acc:
            raise AccountNotFoundError(f"所属账号 #{note.account_id} 不存在")
        if acc.status != "online":
            raise AccountNotOnlineError(f"账号[{acc.name}]当前未登录({acc.status}),发布前需先扫码登录")

        is_ready, missing = validate_publish_ready(note)
        if not is_ready:
            raise PublishNotReadyError(missing)

        if expected_revision is not None and note.revision != expected_revision:
            raise RevisionConflictError(current_rev=note.revision, expected_rev=expected_revision)

        # 解析与校验排期时间
        when: datetime | None = None
        if isinstance(scheduled_at, str) and scheduled_at.strip():
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                try:
                    when = datetime.strptime(scheduled_at.strip(), fmt)
                    break
                except ValueError:
                    continue
            if when is None:
                raise ValueError("时间格式应为 YYYY-MM-DD HH:MM")
        elif isinstance(scheduled_at, datetime):
            when = scheduled_at

        now = datetime.now()
        calc_mode = mode
        if when is not None:
            if when <= now:
                raise ValueError("定时时间必须晚于当前时间")
            gap_h = (when - now).total_seconds() / 3600
            calc_mode = "platform" if gap_h >= config.PLATFORM_SCHEDULE_THRESHOLD_H else "local"
        else:
            calc_mode = "immediate"

        payload_meta = {
            "note_id": note.id,
            "account_id": note.account_id,
            "revision": note.revision,
            "mode": calc_mode,
            "scheduled_at": when.isoformat() if when else None,
            "timezone": timezone,
        }
        payload_hash = _compute_payload_hash(payload_meta)

        # 幂等校验
        if idempotency_key:
            existing = (
                s.query(PublishJob)
                .filter(PublishJob.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                if existing.payload_hash == payload_hash:
                    log("info", "publish", f"命中幂等键[{idempotency_key}],复用任务#{existing.id}")
                    return existing
                raise IdempotencyConflictError(f"幂等键[{idempotency_key}]已存在但请求参数不一致")

        # 检查该笔记是否已有活跃任务
        active = (
            s.query(PublishJob)
            .filter(
                PublishJob.note_id == note.id,
                PublishJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .first()
        )
        if active:
            raise JobConflictError(f"笔记 #{note.id} 已有处于[{active.status}]的任务#{active.id},不可重复排期")

        # 固化快照
        snapshot = {
            "title": note.title,
            "content": note.content,
            "note_type": note.note_type,
            "media_paths": list(note.media_paths or []),
            "tags": list(note.tags or []),
            "revision": note.revision,
        }

        effective_at = now
        if calc_mode == "immediate":
            job_status = "queued"
            note_status = "queued"
            effective_at = now
        elif calc_mode == "local":
            job_status = "scheduled"
            note_status = "scheduled"
            effective_at = apply_jitter(when) if when else now
        else:  # platform
            job_status = "queued"  # 平台定时提交即入平台原生队列,故由本地立即排队去提交
            note_status = "queued"
            effective_at = now

        job = PublishJob(
            note_id=note.id,
            account_id=note.account_id,
            note_revision=note.revision,
            content_snapshot=snapshot,
            mode=calc_mode,
            scheduled_at=when,
            effective_at=effective_at,
            timezone=timezone,
            status=job_status,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            attempt_count=0,
        )
        s.add(job)
        note.status = note_status
        note.schedule_mode = calc_mode
        note.scheduled_at = when
        s.commit()
        s.refresh(job)

        log(
            "info",
            "publish",
            f"创建发布任务#{job.id} 笔记#{note.id} 模式={calc_mode} 计划时间={when or '立即'} 状态={job_status}",
        )

        from ..scheduler import enqueue
        if job_status == "queued":
            enqueue("publish_job", job.id)

        return job

    if session is not None:
        return _do(session)
    with SessionLocal() as s:
        return _do(s)


def cancel_publish_job(job_id: int, session: Session | None = None) -> PublishJob:
    """取消发布任务:仅未提交至平台的任务可安全取消,回退草稿态。"""

    def _do(s: Session) -> PublishJob:
        job = s.get(PublishJob, job_id)
        if not job:
            raise JobNotFoundError(f"任务 #{job_id} 不存在")

        if job.status in ("published", "platform_scheduled"):
            raise CannotCancelError(f"任务已处于平台确认状态[{job.status}],请前往小红书平台手动撤回")

        if job.status in ("publishing", "submitted", "verifying"):
            raise CannotCancelError(f"任务当前正处于[{job.status}]提交/核对中,不可直接取消")

        job.status = "cancelled"
        job.cancel_requested = True
        job.updated_at = datetime.now()

        note = s.get(Note, job.note_id)
        if note and note.status != "published":
            note.status = "draft"
            note.updated_at = datetime.now()

        s.commit()
        s.refresh(job)
        log("info", "publish", f"发布任务#{job.id}已取消,笔记#{job.note_id}已回退为草稿")
        return job

    if session is not None:
        return _do(session)
    with SessionLocal() as s:
        return _do(s)


def recover_stuck_jobs_on_startup() -> dict[str, int]:
    """服务启动时崩溃恢复:
    - 绝不无脑把 publishing 置为 pending 重复发布
    - 检查最新 Attempt 阶段:若已经点击提交(submitting/redirecting),转入 verifying 待核对
    - 若尚未点击提交,可安全放回 queued
    """
    stats = {"to_verifying": 0, "to_queued": 0}
    with SessionLocal() as s:
        stuck_jobs = s.query(PublishJob).filter(PublishJob.status == "publishing").all()
        for job in stuck_jobs:
            latest_attempt = (
                s.query(PublishAttempt)
                .filter(PublishAttempt.job_id == job.id)
                .order_by(PublishAttempt.id.desc())
                .first()
            )
            note = s.get(Note, job.note_id)
            if latest_attempt and latest_attempt.stage in ("submitting", "redirecting", "completed"):
                job.status = "verifying"
                job.last_error_code = "CRASH_DURING_SUBMIT"
                job.last_error_message = "服务中断前已向平台提交,已转入待核对,禁止盲目重发"
                if note:
                    note.status = "verifying"
                    note.verification_status = "uncertain"
                stats["to_verifying"] += 1
                log("warn", "recovery", f"任务#{job.id}可能已提交平台,启动恢复转为 verifying")
            else:
                job.status = "queued"
                if note:
                    note.status = "queued"
                stats["to_queued"] += 1
                log("info", "recovery", f"任务#{job.id}未进入提交阶段,启动恢复重置为 queued")

        # 兜底旧版无 PublishJob 的 Note
        legacy_stuck = s.query(Note).filter(Note.status == "publishing").all()
        for ln in legacy_stuck:
            ln.status = "verifying"
            ln.verification_status = "uncertain"
            stats["to_verifying"] += 1
            log("warn", "recovery", f"历史遗留笔记#{ln.id}发布中断,转为 verifying 待核对")

        s.commit()
    return stats

