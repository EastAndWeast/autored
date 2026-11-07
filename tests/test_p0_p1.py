"""P0 基线与数据保护 & P1 草稿与可靠任务测试套件。

所有测试运行在隔离临时目录中,严格不污染生产 data/ 目录与真实浏览器环境。
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.config as config
from app import models
from app.migrations.runner import get_applied_versions, run_migrations
from app.services.drafts import create_draft, delete_draft, get_draft, update_draft
from app.services.errors import (
    CannotCancelError,
    DraftNotEditableError,
    IdempotencyConflictError,
    JobConflictError,
    PublishNotReadyError,
    RevisionConflictError,
)
from app.services.publish import (
    cancel_publish_job,
    recover_stuck_jobs_on_startup,
    schedule_publish,
    validate_publish_ready,
)


class P0P1TestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="xhs_p0p1_test_"))
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
                name="测试账号-小红",
                status="online",
                platform_region="cn",
                creator_base="https://creator.xiaohongshu.com",
            )
            s.add(acc)
            s.commit()

    # ==================== P0 验证 ====================

    def test_p0_configurable_data_dir(self):
        """[P0] 验证数据目录可动态配置且隔离有效"""
        self.assertTrue(str(config.DATA_DIR).startswith(str(self.test_dir.resolve())))
        self.assertEqual(config.DB_PATH, self.db_path.resolve())
        self.assertTrue(config.UPLOADS_DIR.exists())
        self.assertTrue(config.PROFILES_DIR.exists())

    def test_p0_schema_migrations(self):
        """[P0] 验证数据库 schema 版本与增量迁移机制"""
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        try:
            versions = get_applied_versions(conn)
            self.assertIn(1, versions)
            self.assertIn(2, versions)

            # 验证新字段存在
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(accounts)")
            cols = [row[1] for row in cur.fetchall()]
            self.assertIn("platform_region", cols)
            self.assertIn("timezone", cols)

            cur.execute("PRAGMA table_info(notes)")
            note_cols = [row[1] for row in cur.fetchall()]
            self.assertIn("revision", note_cols)
            self.assertIn("verification_status", note_cols)
        finally:
            conn.close()

    # ==================== P1 草稿与解耦验证 ====================

    def test_p1_create_draft_never_auto_publishes(self):
        """[P1] 验证新建草稿严格为 draft 状态,修订号为 1,绝不自动入队"""
        note = create_draft(
            account_id=1,
            title="美妆新品测评",
            content="这是一篇草稿正文",
            tags=["美妆", "测评"],
        )
        self.assertEqual(note.status, "draft")
        self.assertEqual(note.revision, 1)

        with models.SessionLocal() as s:
            n = s.get(models.Note, note.id)
            self.assertEqual(n.status, "draft")
            self.assertEqual(n.schedule_mode, "immediate")
            # 确认没有生成 publish_jobs
            jobs = s.query(models.PublishJob).filter_by(note_id=note.id).all()
            self.assertEqual(len(jobs), 0)

    def test_p1_create_draft_without_media_allowed(self):
        """[P1] 验证无媒体素材也允许保存草稿"""
        note = create_draft(
            account_id=1,
            title="纯文字草稿灵感",
            content="先记录大纲,后续补充配图",
            media_paths=[],
        )
        self.assertEqual(note.media_paths, [])
        is_ready, missing = validate_publish_ready(note)
        self.assertFalse(is_ready)
        self.assertIn("media_paths", missing)

    def test_p1_update_draft_increments_revision_and_stays_draft(self):
        """[P1] 验证草稿修改只递增 revision 并保持 draft 状态,绝不隐式触发发布"""
        note = create_draft(account_id=1, title="初始标题", content="初始正文")
        self.assertEqual(note.revision, 1)

        updated = update_draft(note.id, title="修改后的标题", content="修改后的正文")
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.status, "draft")
        self.assertEqual(updated.title, "修改后的标题")

    def test_p1_update_draft_optimistic_locking(self):
        """[P1] 验证携带 expected_revision 时若冲突抛出 RevisionConflictError"""
        note = create_draft(account_id=1, title="并发测试")
        # 正确 revision 更新成功
        update_draft(note.id, title="第一次更新", expected_revision=1)

        # 再次更新传入过期的 expected_revision=1 应当拒绝
        with self.assertRaises(RevisionConflictError):
            update_draft(note.id, title="第二次过期更新", expected_revision=1)

    def test_p1_update_published_note_forbidden(self):
        """[P1] 验证已发布或发布中的笔记不可随意被作为草稿修改"""
        note = create_draft(account_id=1, title="已发笔记", media_paths=["pic.png"])
        with models.SessionLocal() as s:
            n = s.get(models.Note, note.id)
            n.status = "published"
            s.commit()

        with self.assertRaises(DraftNotEditableError):
            update_draft(note.id, title="尝试篡改已发笔记")

    # ==================== P1 发布任务与幂等验证 ====================

    def test_p1_schedule_publish_validates_ready(self):
        """[P1] 验证草稿缺少要素时无法安排发布"""
        no_media_note = create_draft(account_id=1, title="无图笔记", media_paths=[])
        with self.assertRaises(PublishNotReadyError) as ctx:
            schedule_publish(no_media_note.id, mode="immediate")
        self.assertIn("media_paths", ctx.exception.missing_fields)

        no_title_note = create_draft(account_id=1, title="", media_paths=["a.png"])
        with self.assertRaises(PublishNotReadyError) as ctx:
            schedule_publish(no_title_note.id, mode="immediate")
        self.assertIn("title", ctx.exception.missing_fields)

    def test_p1_schedule_publish_creates_job_and_snapshot(self):
        """[P1] 验证安排发布时固化内容快照(content_snapshot)"""
        note = create_draft(
            account_id=1,
            title="周末逛吃",
            content="美食分享",
            media_paths=["p1.png"],
            tags=["美食"],
        )
        job = schedule_publish(note.id, mode="immediate")
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.content_snapshot["title"], "周末逛吃")
        self.assertEqual(job.content_snapshot["revision"], 1)

        with models.SessionLocal() as s:
            n = s.get(models.Note, note.id)
            self.assertEqual(n.status, "queued")

    def test_p1_idempotency_same_key_same_payload_returns_existing(self):
        """[P1] 验证相同幂等键 + 相同负载时返回原任务,不重复排期"""
        note = create_draft(account_id=1, title="幂等测试", media_paths=["p1.png"])
        job1 = schedule_publish(note.id, mode="immediate", idempotency_key="idem_key_1001")
        job2 = schedule_publish(note.id, mode="immediate", idempotency_key="idem_key_1001")
        self.assertEqual(job1.id, job2.id)

    def test_p1_idempotency_same_key_diff_payload_raises_conflict(self):
        """[P1] 验证相同幂等键 + 不同负载时抛出 IdempotencyConflictError"""
        note = create_draft(account_id=1, title="幂等冲突测试", media_paths=["p1.png"])
        schedule_publish(note.id, mode="immediate", idempotency_key="idem_key_2001")
        with self.assertRaises(IdempotencyConflictError):
            schedule_publish(note.id, mode="schedule", scheduled_at="2026-11-20 18:00", idempotency_key="idem_key_2001")

    def test_p1_cancel_publish_job_returns_to_draft(self):
        """[P1] 验证取消尚未执行的任务会安全回退为 draft 状态"""
        note = create_draft(account_id=1, title="待取消任务", media_paths=["p1.png"])
        job = schedule_publish(note.id, mode="immediate")
        cancelled_job = cancel_publish_job(job.id)
        self.assertEqual(cancelled_job.status, "cancelled")

        with models.SessionLocal() as s:
            n = s.get(models.Note, note.id)
            self.assertEqual(n.status, "draft")

    def test_p1_cancel_publishing_job_forbidden(self):
        """[P1] 验证处于 publishing 阶段的任务不能随意本地取消"""
        note = create_draft(account_id=1, title="发布中任务", media_paths=["p1.png"])
        job = schedule_publish(note.id, mode="immediate")
        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            j.status = "publishing"
            s.commit()

        with self.assertRaises(CannotCancelError):
            cancel_publish_job(job.id)

    # ==================== P1 崩溃恢复与防重发验证 ====================

    def test_p1_crash_during_submit_transitions_to_verifying(self):
        """[P1 核心防重] 验证在提交阶段(submitting)崩溃时,启动恢复转为 verifying 待核对,绝不盲目重发"""
        note = create_draft(account_id=1, title="崩溃测试-提交中", media_paths=["p1.png"])
        job = schedule_publish(note.id, mode="immediate")

        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            j.status = "publishing"
            n = s.get(models.Note, note.id)
            n.status = "publishing"

            # 模拟已经执行到了 submitting 阶段(已点下发布按钮)
            attempt = models.PublishAttempt(
                job_id=job.id,
                note_id=note.id,
                account_id=1,
                stage="submitting",
                status="running",
            )
            s.add(attempt)
            s.commit()

        # 触发崩溃恢复
        stats = recover_stuck_jobs_on_startup()
        self.assertEqual(stats["to_verifying"], 1)

        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            n = s.get(models.Note, note.id)
            # 状态必须为 verifying,禁止被放入 queued
            self.assertEqual(j.status, "verifying")
            self.assertEqual(n.status, "verifying")
            self.assertEqual(n.verification_status, "uncertain")

    def test_p1_crash_before_submit_safely_requeues(self):
        """[P1] 验证在尚未点击提交阶段(如上传图片时)崩溃,可以安全重新入队"""
        note = create_draft(account_id=1, title="崩溃测试-未提交", media_paths=["p1.png"])
        job = schedule_publish(note.id, mode="immediate")

        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            j.status = "publishing"
            n = s.get(models.Note, note.id)
            n.status = "publishing"

            # 模拟处于 upload_media 阶段(从未点击发布)
            attempt = models.PublishAttempt(
                job_id=job.id,
                note_id=note.id,
                account_id=1,
                stage="upload_media",
                status="running",
            )
            s.add(attempt)
            s.commit()

        stats = recover_stuck_jobs_on_startup()
        self.assertEqual(stats["to_queued"], 1)

        with models.SessionLocal() as s:
            j = s.get(models.PublishJob, job.id)
            n = s.get(models.Note, note.id)
            self.assertEqual(j.status, "queued")
            self.assertEqual(n.status, "queued")


if __name__ == "__main__":
    unittest.main()
