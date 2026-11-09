"""小红书 AI 运营助手 - P3 常驻后台与网页管理端测试套件。

涵盖:
- 跨进程 Profile 互斥锁验证(防止多实例争抢 Chromium Profile)
- 本地定时计划逾期处理(防休眠/关机唤醒后盲目批量补发)
- 逾期任务的人工立即补发恢复机制
- 离线账号草稿保存与发布时登录态校验
- CLI 运维诊断(doctor)与状态检查(status)及 PID 单实例互斥机制
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.config as config
from app import models
from app.cli import PID_FILE, cmd_doctor, cmd_status, get_running_pid
from app.scheduler import scan_due_notes
from app.services.publish import schedule_publish
from app.xhs import profile_lock


class P3TestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="xhs_p3_test_"))
        cls.db_path = cls.test_dir / "app.db"
        config.configure_data_dir(cls.test_dir)
        models.rebind_db(cls.db_path)
        models.init_db(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        models.engine.dispose()
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        config.configure_data_dir(self.test_dir)
        models.rebind_db(self.db_path)
        with models.SessionLocal() as s:
            s.query(models.PublishAttempt).delete()
            s.query(models.PublishJob).delete()
            s.query(models.NoteStat).delete()
            s.query(models.Note).delete()
            s.query(models.Account).delete()
            s.commit()

        # 创建一个测试账号
        with models.SessionLocal() as s:
            acc = models.Account(
                id=1,
                name="P3测试账号",
                status="online",
                platform_region="cn",
                creator_base="https://creator.xiaohongshu.com",
            )
            s.add(acc)
            s.commit()

    def _create_sample_png(self, filename: str = "sample.png") -> str:
        """在受管 uploads 目录生成有效 PNG 签名图片。"""
        p = config.UPLOADS_DIR / filename
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        return filename

    # ==================== 1. 跨进程 Profile 互斥锁 ====================

    def test_profile_lock_acquire_and_release(self):
        """测试同进程及跨进程锁的正常获取与释放。"""
        # 初始应可成功拿锁
        self.assertTrue(profile_lock.acquire(account_id=1, timeout=0.1))
        # 再次获取(同一账号)应互斥失败
        self.assertFalse(profile_lock.acquire(account_id=1, timeout=0.01))
        # 释放锁
        profile_lock.release(account_id=1)
        # 释放后重新拿锁成功
        self.assertTrue(profile_lock.acquire(account_id=1, timeout=0.1))
        profile_lock.release(account_id=1)

    @unittest.skipIf(sys.platform == "win32", "Windows 无 fcntl;锁回退为线程锁 + Chromium 原生 SingletonLock")
    def test_profile_lock_inter_process_simulation(self):
        """模拟另一个外部进程持有 .profile.lock 时,本进程尝试拿锁应立即被拦截。"""
        import fcntl

        lock_path = profile_lock._get_lock_file_path(1)
        # 模拟外部进程通过独立 fd 持有锁
        ext_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(ext_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 此时本进程尝试 acquire 应该失败
            self.assertFalse(profile_lock.acquire(account_id=1, timeout=0.05))
        finally:
            # 外部进程释放锁
            fcntl.flock(ext_fd, fcntl.LOCK_UN)
            os.close(ext_fd)

        # 外部释放后,本进程拿锁成功
        self.assertTrue(profile_lock.acquire(account_id=1, timeout=0.1))
        profile_lock.release(account_id=1)

    # ==================== 2. 过期本地计划防盲目补发 ====================

    def test_overdue_local_schedule_transition_to_overdue(self):
        """验证因电脑休眠/关机导致错过时间超过阈值(60分钟)的任务转为 overdue,绝不盲目发帖。"""
        img_name = self._create_sample_png("test_pic.png")
        now = datetime.now()

        with models.SessionLocal() as s:
            # 创建一篇本地定时笔记
            overdue_note = models.Note(
                account_id=1,
                title="已严重逾期的露营穿搭",
                content="这是由于电脑关机错过的笔记",
                note_type="image",
                media_paths=[img_name],
                status="draft",
                schedule_mode="local",
                scheduled_at=now + timedelta(minutes=10),
                revision=1,
            )
            s.add(overdue_note)
            s.commit()
            overdue_note_id = overdue_note.id

        # 创建定时发布任务
        job = schedule_publish(
            note_id=overdue_note_id,
            mode="schedule",
            scheduled_at=now + timedelta(minutes=10),
        )
        self.assertEqual(job.status, "scheduled")

        # 模拟电脑关机/休眠: 时间流逝, effective_at 变为 2 小时前
        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            j.effective_at = now - timedelta(hours=2)
            j.scheduled_at = now - timedelta(hours=2)
            s.commit()

        # 执行调度扫描
        scan_due_notes()

        with models.SessionLocal() as s:
            fresh_job = s.get(models.PublishJob, job.id)
            fresh_note = s.get(models.Note, overdue_note_id)

            # 核心断言: 状态转为 overdue, 绝不是 queued!
            self.assertEqual(fresh_job.status, "overdue")
            self.assertEqual(fresh_job.last_error_code, "OVERDUE_SKIPPED")
            self.assertEqual(fresh_note.status, "overdue")
            self.assertIn("已错过指定时间", fresh_job.last_error_message)

    def test_on_time_local_schedule_transitions_to_queued(self):
        """验证正常窗口内(如仅过去5分钟)的定时任务正常入队为 queued。"""
        img_name = self._create_sample_png("test_pic2.png")
        now = datetime.now()

        with models.SessionLocal() as s:
            note = models.Note(
                account_id=1,
                title="正常准时笔记",
                content="准时发布的笔记",
                note_type="image",
                media_paths=[img_name],
                status="draft",
                schedule_mode="local",
                scheduled_at=now + timedelta(minutes=10),
                revision=1,
            )
            s.add(note)
            s.commit()
            nid = note.id

        job = schedule_publish(
            note_id=nid,
            mode="schedule",
            scheduled_at=now + timedelta(minutes=10),
        )
        self.assertEqual(job.status, "scheduled")

        # 模拟到期时间仅过去 5 分钟 (正常调度窗口内)
        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            j.effective_at = now - timedelta(minutes=5)
            j.scheduled_at = now - timedelta(minutes=5)
            s.commit()

        # 执行扫描
        scan_due_notes()

        with models.SessionLocal() as s:
            fresh_job = s.get(models.PublishJob, job.id)
            fresh_note = s.get(models.Note, nid)
            # 正常入队
            self.assertEqual(fresh_job.status, "queued")
            self.assertEqual(fresh_note.status, "queued")

    def test_overdue_manual_recovery_to_publish_now(self):
        """验证对已逾期(overdue)的笔记,用户手动点击立即发布可成功恢复排队。"""
        img_name = self._create_sample_png("test_pic3.png")
        now = datetime.now()

        with models.SessionLocal() as s:
            note = models.Note(
                account_id=1,
                title="准备人工补发的逾期笔记",
                content="内容",
                note_type="image",
                media_paths=[img_name],
                status="overdue",
                schedule_mode="local",
                scheduled_at=now - timedelta(hours=3),
                revision=1,
            )
            s.add(note)
            s.commit()
            nid = note.id

        # 用户手动调用立即发布
        recovered_job = schedule_publish(note_id=nid, mode="immediate")
        self.assertEqual(recovered_job.status, "queued")

        with models.SessionLocal() as s:
            fresh_note = s.get(models.Note, nid)
            self.assertEqual(fresh_note.status, "queued")

    def test_offline_account_draft_and_publish_validation(self):
        """验证离线账号可正常存草稿,但在安排发布时严格校验登录态。"""
        from app.services.drafts import create_draft
        from app.services.errors import AccountNotOnlineError

        # 创建一个待扫码的离线账号
        with models.SessionLocal() as s:
            offline_acc = models.Account(
                id=2,
                name="离线草稿号",
                status="pending",
                platform_region="cn",
            )
            s.add(offline_acc)
            s.commit()

        # 1. 存草稿: 允许绑定离线账号, 即使没有图片也成功保存为 draft
        draft = create_draft(
            account_id=2,
            title="离线账号撰写的草稿",
            content="正文内容灵感记录",
            tags=["草稿灵感"],
        )
        self.assertEqual(draft.status, "draft")
        self.assertEqual(draft.account_id, 2)

        # 2. 补上图片
        img_name = self._create_sample_png("offline_test.png")
        with models.SessionLocal() as s:
            n = s.get(models.Note, draft.id)
            n.media_paths = [img_name]
            s.commit()

        # 3. 安排发布: 因账号未登录, 必须抛出 AccountNotOnlineError
        with self.assertRaises(AccountNotOnlineError):
            schedule_publish(note_id=draft.id, mode="immediate")

    # ==================== 3. CLI 运维命令与 PID 机制 ====================

    def test_cli_doctor(self):
        """测试 doctor 命令全面诊断返回 0。"""
        code = cmd_doctor()
        self.assertEqual(code, 0)

    def test_cli_status(self):
        """测试 status 命令正常输出返回 0。"""
        code = cmd_status()
        self.assertEqual(code, 0)

    def test_pid_file_cleanup_and_detection(self):
        """测试单实例 PID 检测与幽灵进程清理。"""
        # 初始无 PID
        self.assertIsNone(get_running_pid())

        # 写入一个肯定不存在的超大 PID (例如 999999)
        PID_FILE.write_text("999999", encoding="utf-8")
        # 再次获取应检测到死进程并自动清理
        self.assertIsNone(get_running_pid())
        self.assertFalse(PID_FILE.exists())

        # 写入当前正在运行的测试进程 PID
        my_pid = os.getpid()
        PID_FILE.write_text(str(my_pid), encoding="utf-8")
        self.assertEqual(get_running_pid(), my_pid)
        PID_FILE.unlink()
