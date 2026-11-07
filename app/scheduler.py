"""调度:发布队列(全局串行)+ 数据采集轮询。

防封设计:
- 全局同一时刻只开一个自动化浏览器(发布/采集共用一条队列)
- 相邻任务随机间隔 5~15 分钟
- 本地定时发布带 0~10 分钟随机抖动
- 每账号每日发布上限,超限自动顺延到第二天同一时刻
- 距今 ≥2.5h 的定时走平台原生定时(提交即入平台队列,不依赖本进程)
"""
from __future__ import annotations

import asyncio
import random
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .models import Account, Note, NoteStat, PublishAttempt, PublishJob, SessionLocal, log
from .xhs.publisher import publish_note
from .services.publish import recover_stuck_jobs_on_startup

queue: "asyncio.Queue[tuple[str, int]] | None" = None  # 在 worker 循环内创建(3.9 的 Queue 绑定 loop)

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_worker_thread: threading.Thread | None = None
_stop = threading.Event()
_loop: asyncio.AbstractEventLoop | None = None
_orphan: list[tuple[str, int]] = []  # worker 未启动前的兜底暂存


def enqueue(kind: str, item_id: int) -> None:
    """线程安全入队(立即发布/手动刷新从 API 线程调用)。"""
    if _loop is not None and _loop.is_running() and queue is not None:
        _loop.call_soon_threadsafe(queue.put_nowait, (kind, item_id))
    else:
        _orphan.append((kind, item_id))


# ---------- 扫描待发布 ----------

def scan_due_notes() -> int:
    """扫描到期任务:
    - 扫描已到期(scheduled)的 PublishJob 并置为 queued 入队
    - 兼容扫描历史 pending 笔记并创建对应 PublishJob
    - 到期的 platform_scheduled 自动流转为 published(带 unverified 待核对标记)
    """
    n = 0
    now = datetime.now()
    with SessionLocal() as s:
        # 1. 平台原生定时笔记到达设定时间:自动流转为 published
        plat_due = (
            s.query(Note)
            .filter(
                Note.status == "platform_scheduled",
                Note.scheduled_at.isnot(None),
                Note.scheduled_at <= now,
            )
            .all()
        )
        for pn in plat_due:
            pn.status = "published"
            pn.verification_status = "unverified"
            pn.published_at = pn.scheduled_at or now

        # 2. 持久化 PublishJob 扫描(仅到期的 scheduled 任务,已是 queued 的由内存队列维护不重复入队)
        due_jobs = (
            s.query(PublishJob)
            .filter(
                PublishJob.status == "scheduled",
                PublishJob.effective_at <= now,
            )
            .all()
        )
        for job in due_jobs:
            # 逾期检查: 如果超过设定时间超过阈值(如休眠/关机),转为 overdue 暂停,防开机批量补发
            gap_seconds = (now - job.effective_at).total_seconds()
            if job.mode == "local" and gap_seconds > (config.OVERDUE_THRESHOLD_MINUTES * 60):
                job.status = "overdue"
                job.last_error_code = "OVERDUE_SKIPPED"
                job.last_error_message = f"本地计划已错过指定时间 {int(gap_seconds // 60)} 分钟,已自动暂停,请确认后重新排期或立即发布"
                note = s.get(Note, job.note_id)
                if note:
                    note.status = "overdue"
                    note.error = job.last_error_message
                log("warn", "scheduler", f"任务#{job.id}(笔记#{job.note_id})已逾期 {int(gap_seconds // 60)} 分钟,转为 overdue,防批量补发", session=s)
                continue

            job.status = "queued"
            note = s.get(Note, job.note_id)
            if note and note.status != "queued":
                note.status = "queued"
            enqueue("publish_job", job.id)
            n += 1

        # 3. 兼容历史遗留的 pending 笔记
        legacy_notes = s.query(Note).filter(Note.status == "pending").all()
        for note in legacy_notes:
            due = False
            if note.schedule_mode == "immediate":
                due = True
            elif note.schedule_mode in ("local", "platform") and note.scheduled_at:
                due = note.scheduled_at <= now
            if due:
                if note.schedule_mode == "local" and note.scheduled_at and (now - note.scheduled_at).total_seconds() > (config.OVERDUE_THRESHOLD_MINUTES * 60):
                    note.status = "overdue"
                    note.error = "本地计划已错过发布时间,已自动暂停"
                    continue
                note.status = "queued"
                enqueue("publish", note.id)
                n += 1

        s.commit()
    return n


# ---------- 队列 worker ----------

def _run_worker_loop() -> None:
    """单线程串行消费;每任务在自己的事件循环里跑 playwright。"""
    global _loop, queue
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    queue = asyncio.Queue()
    # 兜底孤儿任务
    for item in list(_orphan):
        _loop.call_soon_threadsafe(queue.put_nowait, item)
    _orphan.clear()

    async def _worker():
        # 启动时先扫描一次存量
        _loop.run_in_executor(None, scan_due_notes)
        while not _stop.is_set():
            try:
                kind, item_id = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            did_work = False
            try:
                if kind in ("publish_job", "publish"):
                    did_work = await _do_publish(item_id) or False
                elif kind == "collect":
                    did_work = await _do_collect(item_id) or False
            except Exception as e:  # noqa: BLE001
                did_work = True
                log("error", "worker", f"任务 {kind}#{item_id} 异常: {e}")
            # 错峰机制: 仅发布发帖任务执行 5~15 分钟长休眠防封; 采集任务仅需 5 秒安全冷却, 避免阻塞其它任务
            if did_work:
                if kind in ("publish_job", "publish"):
                    gap = random.uniform(*config.INTER_ACCOUNT_DELAY)
                    log("info", "worker", f"发布任务完成,错峰休眠 {gap/60:.1f} 分钟后处理下一个")
                    await asyncio.sleep(gap)
                else:
                    await asyncio.sleep(5)

    try:
        _loop.run_until_complete(_worker())
    except Exception as e:  # noqa: BLE001
        log("error", "worker", f"worker 线程致命退出: {e}")


async def _do_publish(item_id: int) -> bool:
    from .xhs import profile_lock

    with SessionLocal() as s:
        # 智能适配:支持传入 PublishJob.id 或直接传入 Note.id
        job = s.get(PublishJob, item_id)
        if job is not None and s.get(Note, job.note_id) is not None:
            note = s.get(Note, job.note_id)
        else:
            note = s.get(Note, item_id)
            if note is not None:
                job = (
                    s.query(PublishJob)
                    .filter(PublishJob.note_id == note.id)
                    .order_by(PublishJob.id.desc())
                    .first()
                )
            else:
                return False

        if note is None or note.status not in ("queued",):
            return False

        account = s.get(Account, note.account_id)
        if account is None:
            return False

        # 每日限额:超限则顺延一天
        today = datetime.now().date()
        published_today = (
            s.query(Note)
            .filter(
                Note.account_id == account.id,
                Note.status == "published",
                Note.published_at >= datetime.combine(today, datetime.min.time()),
            )
            .count()
        )
        if published_today >= account.daily_limit:
            postpone = apply_jitter((note.scheduled_at or datetime.now()) + timedelta(days=1))
            note.status = "pending"
            note.schedule_mode = "local"
            note.scheduled_at = postpone
            if job:
                job.status = "scheduled"
                job.mode = "local"
                job.scheduled_at = postpone
                job.effective_at = postpone
            s.commit()
            log("warn", "publish", f"账号[{account.name}]今日已达上限({account.daily_limit}),笔记#{note.id}顺延至 {postpone:%m-%d %H:%M}")
            return False

        if not profile_lock.acquire(account.id):
            note.status = "pending"
            if job:
                job.status = "queued"
            s.commit()
            log("info", "publish", f"账号[{account.name}]正被登录/检测占用,笔记#{note.id}稍后重试")
            return False

        note.status = "publishing"
        if job:
            job.status = "publishing"
            job.attempt_count += 1

        # 创建 PublishAttempt 记录本次尝试
        attempt = None
        if job:
            attempt = PublishAttempt(
                job_id=job.id,
                note_id=note.id,
                account_id=account.id,
                attempt_no=job.attempt_count,
                started_at=datetime.now(),
                stage="init",
                status="running",
            )
            s.add(attempt)
        s.commit()
        attempt_id = attempt.id if attempt else None
        job_id = job.id if job else None
        note_id = note.id

    def on_stage(stage_name: str) -> None:
        if attempt_id:
            with SessionLocal() as s_stage:
                att = s_stage.get(PublishAttempt, attempt_id)
                if att:
                    att.stage = stage_name
                    s_stage.commit()

    try:
        headless = get_setting_bool("headless", True)
        result = await publish_note(note, account, headless=headless, on_stage=on_stage)
        with SessionLocal() as s:
            fresh = s.get(Note, note_id)
            fresh_job = s.get(PublishJob, job_id) if job_id else None
            fresh_att = s.get(PublishAttempt, attempt_id) if attempt_id else None

            is_platform = (fresh_job.mode == "platform") if fresh_job else (fresh and fresh.schedule_mode == "platform")
            new_status = "platform_scheduled" if is_platform else "published"

            if fresh:
                fresh.status = new_status
                fresh.submitted_at = datetime.now()
                if not is_platform:
                    fresh.confirmed_at = datetime.now()
                    fresh.published_at = datetime.now()
                    fresh.verification_status = "verified"
                else:
                    fresh.verification_status = "unverified"
                fresh.xhs_note_id = result.get("xhs_note_id")
                fresh.xhs_publish_url = result.get("xhs_publish_url")
                fresh.xhs_url = result.get("xhs_url")
                fresh.error = None

            if fresh_job:
                fresh_job.status = new_status
                fresh_job.last_error_code = None
                fresh_job.last_error_message = None

            if fresh_att:
                fresh_att.status = "success"
                fresh_att.stage = "completed"
                fresh_att.ended_at = datetime.now()
                fresh_att.platform_note_id = result.get("xhs_note_id")
                fresh_att.success_url = result.get("xhs_publish_url")
                fresh_att.detail_url = result.get("xhs_url")

            acc = s.get(Account, account.id)
            if acc:
                acc.last_publish_at = datetime.now()

            s.commit()
    except Exception as e:  # noqa: BLE001
        with SessionLocal() as s:
            fresh = s.get(Note, note_id)
            fresh_job = s.get(PublishJob, job_id) if job_id else None
            fresh_att = s.get(PublishAttempt, attempt_id) if attempt_id else None

            # 核心防重逻辑:若在点击发布按钮阶段或等待跳转阶段中断,绝对不盲目重发,转为待核对(verifying)
            was_submitted = fresh_att and fresh_att.stage in ("submitting", "redirecting")

            err_msg = _sanitize_error(e)
            if was_submitted:
                if fresh:
                    fresh.status = "verifying"
                    fresh.verification_status = "uncertain"
                    fresh.error = f"提交中断(待核对): {err_msg}"
                if fresh_job:
                    fresh_job.status = "verifying"
                    fresh_job.last_error_code = "UNCERTAIN_SUBMIT"
                    fresh_job.last_error_message = err_msg
                if fresh_att:
                    fresh_att.status = "uncertain"
                    fresh_att.ended_at = datetime.now()
                    fresh_att.error_code = type(e).__name__
                    fresh_att.error_message = err_msg
                log("warn", "publish", f"笔记#{note_id}在提交阶段异常,转为待核对(verifying): {e}")
                # 自动核实:去平台笔记列表按标题查找,存在即转 published
                asyncio.ensure_future(
                    _auto_verify_uncertain(note_id, account.id, note.title)
                )
            else:
                if fresh:
                    fresh.status = "failed"
                    fresh.error = err_msg
                if fresh_job:
                    fresh_job.status = "failed"
                    fresh_job.last_error_code = type(e).__name__
                    fresh_job.last_error_message = err_msg
                if fresh_att:
                    fresh_att.status = "failed"
                    fresh_att.ended_at = datetime.now()
                    fresh_att.error_code = type(e).__name__
                    fresh_att.error_message = err_msg
                log("error", "publish", f"笔记#{note_id}发布失败(未提交): {e}")

            s.commit()
    finally:
        profile_lock.release(account.id)
    return True


async def _auto_verify_uncertain(note_id: int, account_id: int, title: str) -> None:
    """提交中断的自动核实:平台列表里找到该标题 → 转 published。"""
    from .xhs.publisher import verify_note_on_platform
    from datetime import datetime as _dt

    await asyncio.sleep(10)  # 给平台入库留点时间
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None or note.status != "verifying":
            return  # 期间已被人工处理或核实过
        account = s.get(Account, account_id)
        if account is None:
            return
    try:
        res = await verify_note_on_platform(account, title.strip())
    except Exception as e:  # noqa: BLE001
        log("warn", "verify", f"笔记#{note_id}自动核实异常: {e}")
        return
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None or note.status != "verifying":
            return
        if res.get("found"):
            published_at = None
            raw = res.get("published_at")
            if raw:
                try:
                    published_at = _dt.strptime(raw, "%Y-%m-%d %H:%M")
                except ValueError:
                    published_at = None
            note.status = "published"
            note.published_at = published_at or note.published_at or _dt.now()
            note.verification_status = "confirmed"
            note.error = None
            s.commit()
            log("info", "verify", f"笔记#{note_id}自动核实成功:平台已存在({raw}),转 published")
        elif res.get("found") is False:
            note.verification_status = "not_found"
            s.commit()
            log("warn", "verify", f"笔记#{note_id}核实:平台列表未见该标题,请人工确认后处理")
        else:
            log("warn", "verify", f"笔记#{note_id}核实未完成({res.get('reason')}),保持待核对")


async def _do_collect(account_id: int) -> bool:
    from .xhs.stats_collector import collect_account_stats as _c  # 延迟导入避免循环
    from .xhs import profile_lock

    with SessionLocal() as s:
        account = s.get(Account, account_id)
        if account is None or account.status != "online":
            return False
        name = account.name

    if not profile_lock.acquire(account_id):
        log("info", "stats", f"账号[{name}]正被登录/检测占用,本次采集跳过")
        return False
    rows: list[dict] = []
    error_msg: str | None = None
    try:
        rows = await _c(account)
    except PermissionError:
        with SessionLocal() as s:
            acc = s.get(Account, account_id)
            if acc:
                acc.status = "expired"
                acc.last_stats_error = "登录态失效,采集中止"
                s.commit()
        log("warn", "stats", f"账号[{name}]登录态失效,采集中止")
        return True
    except Exception as e:  # noqa: BLE001
        error_msg = _sanitize_error(e)
        log("error", "stats", f"账号[{name}]采集失败: {e}")
    finally:
        profile_lock.release(account_id)

    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        now = datetime.now()
        if acc:
            acc.last_stats_at = now
            if error_msg:
                acc.last_stats_error = error_msg
            elif len(rows) == 0:
                acc.last_stats_error = "创作者中心返回 0 条笔记(可能为新账号或尚无已审核发布内容)"
            else:
                acc.last_stats_error = None

        saved = 0
        ambiguous_count = 0
        for row in rows:
            note, matched_by = _match_note(s, account_id, row)
            if note is None:
                if matched_by == "ambiguous":
                    ambiguous_count += 1
                continue
            if note.status == "platform_scheduled":
                note.status = "published"  # 平台定时笔记已产出统计数据,流转为已发布
                note.verification_status = "verified"
            s.add(
                NoteStat(
                    note_id=note.id,
                    fetched_at=now,
                    impression=row.get("impression", 0),
                    read=row.get("read", 0),
                    like=row.get("like", 0),
                    collect=row.get("collect", 0),
                    comment=row.get("comment", 0),
                    share=row.get("share", 0),
                    fans_gain=row.get("fans_gain", 0),
                    source=row.get("source", "dom"),
                    raw_matched_by=matched_by,
                )
            )
            saved += 1
        s.commit()

    if ambiguous_count > 0:
        log("warn", "stats", f"账号[{name}]有 {ambiguous_count} 篇存在同名歧义,已按安全约定跳过自动绑定")
    log("info", "stats", f"账号[{name}]入库 {saved} 条快照")
    return True


def _match_note(s, account_id: int, row: dict) -> tuple[Note | None, str | None]:
    """按 note_id 优先精确匹配; 标题作为受控兜底, 存在同名歧义时严禁盲目绑定。
    返回 (note, matched_by): matched_by 为 'platform_id' | 'title' | 'ambiguous' | None
    """
    nid = str(row.get("note_id") or "").strip()
    if nid:
        found = (
            s.query(Note)
            .filter(Note.account_id == account_id, Note.xhs_note_id == nid)
            .first()
        )
        if found:
            return found, "platform_id"

    title = (row.get("title") or "").strip()
    if title:
        candidates = (
            s.query(Note)
            .filter(Note.account_id == account_id, Note.title == title)
            .all()
        )
        if len(candidates) == 1:
            note = candidates[0]
            # 受控兜底绑定: 如果本地尚无 xhs_note_id 但抓到了,顺带回填
            if not note.xhs_note_id and nid:
                note.xhs_note_id = nid
            return note, "title"
        elif len(candidates) > 1:
            # 歧义保护: 同账号存在多篇同名笔记, 禁止盲目张冠李戴
            return None, "ambiguous"

    return None, None


# ---------- 工具 ----------

def apply_jitter(when: datetime) -> datetime:
    """本地定时统一加 0~10 分钟随机抖动,避免整点批量发布的机器特征。"""
    from datetime import timedelta as _td
    return when + _td(seconds=random.randint(*config.SCHEDULE_JITTER))


def _sanitize_error(e: Exception) -> str:
    """错误串去掉本地绝对路径再入库/回显(S-06)。"""
    msg = str(e)[:500]
    return msg.replace(str(config.BASE_DIR), "<项目目录>")


# ---------- 定时任务 ----------

def _job_scan() -> None:
    try:
        scan_due_notes()
    except Exception as e:  # noqa: BLE001
        log("error", "scheduler", f"扫描待发布异常: {e}")


def _job_collect_all() -> None:
    """按设置的时间点采集所有在线账号(先入队,由 worker 串行执行)。"""
    with SessionLocal() as s:
        ids = [a.id for a in s.query(Account).filter(Account.status == "online").all()]
    for aid in ids:
        enqueue("collect", aid)


def _add_collect_cron_jobs() -> None:
    times = get_collect_times()
    for i, t in enumerate(times):
        try:
            hh, mm = t.split(":")
            jitter_mm = (int(mm) + i * 7 + random.randint(0, 5)) % 60
            _scheduler.add_job(
                _job_collect_all,
                CronTrigger(hour=int(hh), minute=jitter_mm),
                id=f"collect_{i}",
            )
        except Exception as e:  # noqa: BLE001
            log("warn", "scheduler", f"忽略非法采集时间配置[{t}]: {e}")


def _setup_jobs() -> None:
    _scheduler.add_job(_job_scan, "interval", seconds=30, id="scan_due")
    _add_collect_cron_jobs()


def reload_collect_jobs() -> None:
    """设置修改后重排采集定时任务(F-08.2, R-04)。"""
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("collect_"):
            job.remove()
    _add_collect_cron_jobs()
    log("info", "scheduler", f"采集计划已更新: {get_collect_times()}")


def start() -> None:
    global _worker_thread
    # 启动时崩溃恢复:按最新 Attempt 阶段智能识别,已提交平台转 verifying,禁止盲目重发
    recovery_stats = recover_stuck_jobs_on_startup()
    log("info", "scheduler", f"启动恢复完成: {recovery_stats['to_verifying']} 任务转待核对, {recovery_stats['to_queued']} 任务重入队")

    # 上次进程退出时卡在登录中的账号复位为待扫码
    with SessionLocal() as s:
        stuck_accs = s.query(Account).filter(Account.status == "logging_in").all()
        for a in stuck_accs:
            a.status = "pending"
            log("warn", "scheduler", f"账号[{a.name}]上次登录未完成,已复位为待扫码")
        s.commit()

    _worker_thread = threading.Thread(target=_run_worker_loop, name="xhs-worker", daemon=True)
    _worker_thread.start()
    _setup_jobs()
    _scheduler.start()
    log("info", "scheduler", "调度器已启动")


def shutdown() -> None:
    _stop.set()
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------- 设置读写 ----------

def get_setting(key: str, default: str = "") -> str:
    with SessionLocal() as s:
        from .models import Setting
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    from .models import Setting
    with SessionLocal() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


def get_setting_bool(key: str, default: bool) -> bool:
    return get_setting(key, "1" if default else "0") == "1"


def get_collect_times() -> list[str]:
    raw = get_setting("collect_times", ",".join(config.DEFAULT_COLLECT_TIMES))
    times = [t.strip() for t in raw.split(",") if t.strip()]
    return times or config.DEFAULT_COLLECT_TIMES
