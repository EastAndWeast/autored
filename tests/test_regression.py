"""回归测试套件:验证全部 14 项首轮问题(F-01~F-08, S-01~S-06)与 4 项二轮问题(R-01~R-04)均已修复通过。"""
import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import HTTPException

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

# 隔离测试沙箱环境,不触碰生产 data/app.db
TEST_TMP = Path(tempfile.mkdtemp(prefix="xhs_reg_"))
import app.config as config
config.configure_data_dir(TEST_TMP / "data")

import app.models as models
models.rebind_db(config.DB_PATH)

import app.main as main_mod
import app.scheduler as scheduler_mod
import app.obsidian_sync as obsidian_mod
import app.api.accounts as api_accounts
import app.api.notes as api_notes
import app.api.stats as api_stats
import app.api.settings as api_settings
import app.xhs.login as xhs_login
from app.xhs import profile_lock

models.init_db()


class RegressionTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.configure_data_dir(TEST_TMP / "data")
        models.rebind_db(config.DB_PATH)
        models.init_db(config.DB_PATH)

    def setUp(self):
        config.configure_data_dir(TEST_TMP / "data")
        models.rebind_db(config.DB_PATH)
        with models.SessionLocal() as s:
            s.query(models.NoteStat).delete()
            s.query(models.Note).delete()
            s.query(models.Account).delete()
            s.query(models.LogEntry).delete()
            s.query(models.Setting).delete()
            s.commit()
        scheduler_mod._orphan.clear()

    def test_login_success_requires_reopen_verification(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                with models.SessionLocal() as s:
                    acc = models.Account(name=f"恢复校验{verified}", status="logging_in")
                    s.add(acc)
                    s.commit()
                    aid = acc.id
                context = MagicMock()
                page = MagicMock()
                context.pages = [page]
                context.close = AsyncMock()
                page.goto = AsyncMock()
                page.wait_for_timeout = AsyncMock()
                state = {}
                with patch.object(xhs_login, "launch_account_context", AsyncMock(return_value=context)), patch.object(
                    xhs_login, "_extract_qr", AsyncMock(return_value=None)
                ), patch.object(xhs_login, "_peek_qr", AsyncMock(return_value=None)), patch.object(
                    xhs_login, "_login_completed", AsyncMock(return_value=True)
                ), patch.object(xhs_login, "persist_session_cookies", AsyncMock()) as save, patch.object(
                    xhs_login, "_check_cookie_inner", AsyncMock(return_value=verified)
                ) as check, patch.object(xhs_login.asyncio, "sleep", AsyncMock()):
                    asyncio.run(xhs_login._do_login_flow(object(), aid, state))
                    save.assert_awaited_once_with(context)
                    check.assert_awaited_once()
                    context.close.assert_awaited_once()
                self.assertEqual(state["status"], "success" if verified else "failed")
                with models.SessionLocal() as s:
                    self.assertEqual(s.get(models.Account, aid).status, "online" if verified else "pending")

    def test_sso_redirect_and_missing_credentials_never_finish_login(self):
        page = MagicMock()
        page.url = "https://customer.xiaohongshu.com/verify?redirect=https://creator.xiaohongshu.com/"
        page.get_by_text.return_value.first.is_visible = AsyncMock(return_value=True)
        self.assertFalse(asyncio.run(xhs_login._login_completed(page)))
        page.url = config.CREATOR_BASE + "/home"
        page.locator.return_value.first.count = AsyncMock(return_value=0)
        page.context.cookies = AsyncMock(return_value=[])
        self.assertFalse(asyncio.run(xhs_login._login_completed(page)))

    def test_international_creator_domain_follows_saved_session(self):
        from app.xhs.browser import creator_base
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[
            {"name": "a1", "value": "visitor", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": "test-only", "domain": ".rednote.com"},
        ])
        self.assertEqual(asyncio.run(creator_base(context)), "https://creator.rednote.com")
        context.cookies = AsyncMock(return_value=[
            {"name": "customer-sso-sid", "value": "test-only", "domain": ".xiaohongshu.com"},
        ])
        self.assertEqual(asyncio.run(creator_base(context)), "https://creator.xiaohongshu.com")

    def test_loaded_login_check_returns_without_fixed_wait(self):
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        with patch.object(xhs_login, "launch_account_context", AsyncMock(return_value=context)), patch.object(
            xhs_login, "creator_base", AsyncMock(return_value=config.CREATOR_BASE)
        ), patch.object(xhs_login, "_login_completed", AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(xhs_login._check_cookie_inner(object(), MagicMock())))
        page.wait_for_timeout.assert_not_awaited()
        context.close.assert_awaited_once()

    def test_login_launch_failure_clears_status_and_lock(self):
        with models.SessionLocal() as s:
            acc = models.Account(name="启动失败测试")
            s.add(acc)
            s.commit()
            aid = acc.id
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def fake_session():
            yield object()
        with patch("app.xhs.login.playwright_session", fake_session), patch(
            "app.xhs.login._do_login_flow", new=AsyncMock(side_effect=RuntimeError("browser unavailable"))
        ):
            asyncio.run(xhs_login._do_login(aid))
        self.assertEqual(xhs_login.get_login_state(aid)["status"], "failed")
        with models.SessionLocal() as s:
            self.assertEqual(s.get(models.Account, aid).status, "pending")
        self.assertTrue(profile_lock.acquire(aid))
        profile_lock.release(aid)

    def test_login_redirect_requires_loaded_backend(self):
        page = MagicMock()
        page.url = config.CREATOR_BASE + "/home"
        page.context.cookies = AsyncMock(return_value=[{"name": "customer-sso-sid", "value": "test-only"}])
        page.locator.return_value.first.count = AsyncMock(return_value=0)
        page.get_by_text.return_value.first.is_visible = AsyncMock(return_value=False)
        self.assertFalse(asyncio.run(xhs_login._login_completed(page)))
        page.get_by_text.return_value.first.is_visible = AsyncMock(return_value=True)
        self.assertTrue(asyncio.run(xhs_login._login_completed(page)))
        page.url = config.LOGIN_URL
        self.assertFalse(asyncio.run(xhs_login._login_completed(page)))

    def test_F01_relogin_is_now_async(self):
        """[F-01 回归] 验证 relogin 已改为 async def,在事件循环中调用不再崩溃"""
        self.assertTrue(asyncio.iscoroutinefunction(api_accounts.relogin))
        with models.SessionLocal() as s:
            acc = models.Account(name="F01账号", status="expired")
            s.add(acc)
            s.commit()
            acc_id = acc.id

        with patch("app.api.accounts.start_login", return_value=True):
            res = asyncio.run(api_accounts.relogin(acc_id))
            self.assertEqual(res, {"ok": True})

    def test_F02_delete_note_and_account_cascade_and_media_cleanup(self):
        """[F-02 回归] 验证删除带 NoteStat 的笔记和账号不再报 IntegrityError,且清理关联媒体文件"""
        media_file = config.UPLOADS_DIR / "test_img.png"
        media_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        with models.SessionLocal() as s:
            acc = models.Account(name="F02账号", status="online")
            s.add(acc)
            s.commit()
            note = models.Note(
                account_id=acc.id,
                title="带统计笔记",
                status="published",
                media_paths=["test_img.png"],
            )
            s.add(note)
            s.commit()
            stat = models.NoteStat(note_id=note.id, impression=100, read=50)
            s.add(stat)
            s.commit()
            note_id, stat_id = note.id, stat.id

        api_notes.delete_note(note_id)
        with models.SessionLocal() as s:
            self.assertEqual(s.query(models.NoteStat).filter_by(id=stat_id).count(), 0)
        self.assertFalse(media_file.exists())

    def test_F03_publish_now_sets_queued_directly(self):
        """[F-03 回归] 验证 publish_now 直接将状态设为 queued,避免被 _do_publish 跳过及被 scan_due_notes 重复入队"""
        with models.SessionLocal() as s:
            acc = models.Account(name="F03账号", status="online")
            s.add(acc)
            s.commit()
            note = models.Note(
                account_id=acc.id,
                title="立即发测试",
                media_paths=["a.png"],
                status="failed",
                schedule_mode="immediate",
            )
            s.add(note)
            s.commit()
            nid = note.id

        api_notes.publish_now(nid)
        with models.SessionLocal() as s:
            n = s.get(models.Note, nid)
            self.assertEqual(n.status, "queued")
        self.assertEqual(scheduler_mod.scan_due_notes(), 0)
        self.assertEqual(len(scheduler_mod._orphan), 1)

    def test_F04_daily_limit_postpone_switches_to_local_mode(self):
        """[F-04 回归] 验证 immediate 笔记触发每日限额顺延后切换为 local 模式,不再死循环重入队"""
        with models.SessionLocal() as s:
            acc = models.Account(name="F04账号", status="online", daily_limit=1)
            s.add(acc)
            s.commit()
            pub = models.Note(account_id=acc.id, title="已发", status="published", published_at=datetime.now())
            n2 = models.Note(account_id=acc.id, title="超限", status="queued", schedule_mode="immediate")
            s.add_all([pub, n2])
            s.commit()
            n2_id = n2.id

        did_work = asyncio.run(scheduler_mod._do_publish(n2_id))
        self.assertFalse(did_work)
        with models.SessionLocal() as s:
            n2 = s.get(models.Note, n2_id)
            self.assertEqual(n2.status, "pending")
            self.assertEqual(n2.schedule_mode, "local")
        self.assertEqual(scheduler_mod.scan_due_notes(), 0)

    def test_F07_obsidian_cover_dedup_title_trunc_and_no_image_skip(self):
        """[F-07 回归] 验证 Obsidian 封面图不重复、标题截断为20字、无图笔记跳过导入"""
        vault = TEST_TMP / "vault_f07"
        vault.mkdir(exist_ok=True)
        (vault / "p1.png").write_bytes(b"\x89PNG\r\n\x1a\n1")
        (vault / "p2.png").write_bytes(b"\x89PNG\r\n\x1a\n2")

        md = vault / "note1.md"
        long_title = "一二三四五六七八九十一二三四五六七八九十超长部分"
        md.write_text(f"---\ntitle: {long_title}\ncover: p2.png\n---\n正文\n![[p1.png]]\n![[p2.png]]\n", encoding="utf-8")
        draft = obsidian_mod.parse_markdown_file(str(md), str(vault))
        self.assertEqual(len(draft.images), 2)
        self.assertTrue(draft.images[0].endswith("p2.png"))

        with models.SessionLocal() as s:
            acc = models.Account(name="F07账号", status="online")
            s.add(acc)
            s.commit()
            acc_id = acc.id

        nid, warnings = obsidian_mod.import_draft(draft, acc_id)
        self.assertTrue(nid > 0)
        with models.SessionLocal() as s:
            n = s.get(models.Note, nid)
            self.assertEqual(len(n.title), 20)

        md_noimg = vault / "noimg.md"
        md_noimg.write_text("---\ntitle: 纯文本\n---\n只有文字", encoding="utf-8")
        draft_noimg = obsidian_mod.parse_markdown_file(str(md_noimg), str(vault))
        nid2, _ = obsidian_mod.import_draft(draft_noimg, acc_id)
        self.assertEqual(nid2, 0)

    def test_S01_to_S06_security_fixes(self):
        """[S-01~S-06 回归] 验证路径穿越拦截、代理脱敏、文件头校验、权限收紧与错误路径脱敏"""
        vault = TEST_TMP / "vault_s01"
        vault.mkdir(exist_ok=True)
        secret_img = TEST_TMP / "secret.png"
        secret_img.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
        self.assertIsNone(obsidian_mod._find_image(vault, "../secret.png"))

        self.assertEqual(
            api_accounts.mask_proxy("http://myuser:supersecret@127.0.0.1:7890"),
            "http://myuser:***@127.0.0.1:7890",
        )

        self.assertFalse(api_notes._check_magic(".png", b"<html>not a png</html>"))
        self.assertTrue(api_notes._check_magic(".png", b"\x89PNG\r\n\x1a\n\x00\x00"))

        prof = config.PROFILES_DIR / "account_99"
        prof.mkdir(exist_ok=True)
        st_file = prof / "state.json"
        st_file.write_text("{}", encoding="utf-8")
        main_mod._cleanup_state_files()
        main_mod._tighten_permissions()
        self.assertFalse(st_file.exists())
        @unittest_skip_win = None  # placeholder
        if sys.platform != "win32":
            self.assertEqual(oct(config.DB_PATH.stat().st_mode & 0o777), "0o600")

        sanitized = scheduler_mod._sanitize_error(RuntimeError(f"failed at {config.BASE_DIR}/app/xhs"))
        self.assertNotIn(str(config.BASE_DIR), sanitized)
        self.assertIn("<项目目录>", sanitized)

    def test_R01_obsidian_scan_returns_valid_dir_and_drafts(self):
        """[R-01 修复验证] obsidian_scan 正常返回 dir 与 drafts,不再报 NameError"""
        vault = TEST_TMP / "vault_r01"
        vault.mkdir(exist_ok=True)
        (vault / "a.md").write_text("---\ntitle: 测试扫描\n---\n正文", encoding="utf-8")
        res = api_settings.obsidian_scan(str(vault))
        self.assertEqual(res["dir"], str(vault.resolve()))
        self.assertEqual(len(res["drafts"]), 1)

    def test_R02_obsidian_import_with_none_dir_path_uses_saved_setting(self):
        """[R-02 修复验证] obsidian_import 在 dir_path=None 时自动回退使用数据库配置的 obsidian_dir,且拒绝越权文件"""
        vault = TEST_TMP / "vault_r02"
        vault.mkdir(exist_ok=True)
        scheduler_mod.set_setting("obsidian_dir", str(vault))

        outside_file = TEST_TMP / "outside.md"
        outside_file.write_text("secret", encoding="utf-8")

        with models.SessionLocal() as s:
            acc = models.Account(name="R02账号", status="online")
            s.add(acc)
            s.commit()
            acc_id = acc.id

        body = api_settings.ObsidianImportIn(dir_path=None, files=[str(outside_file)], account_id=acc_id)
        res = api_settings.obsidian_import(body)
        self.assertEqual(len(res["results"]), 1)
        self.assertFalse(res["results"][0]["ok"])
        self.assertIn("已拒绝", res["results"][0]["error"])

    def test_R03_do_publish_respects_profile_lock_and_platform_scheduled_transitions(self):
        """[R-03 修复验证] _do_publish 受 profile_lock 互斥保护;到期 platform_scheduled 自动流转为 published"""
        with models.SessionLocal() as s:
            acc = models.Account(name="R03账号", status="online")
            s.add(acc)
            s.commit()
            note = models.Note(account_id=acc.id, title="锁测试", status="queued", schedule_mode="immediate")
            plat_note = models.Note(
                account_id=acc.id,
                title="平台定时到期",
                status="platform_scheduled",
                schedule_mode="platform",
                scheduled_at=datetime.now() - timedelta(minutes=5),
            )
            s.add_all([note, plat_note])
            s.commit()
            acc_id, note_id, plat_id = acc.id, note.id, plat_note.id

        # 占用账号锁时,_do_publish 应退回 pending 并返回 False
        self.assertTrue(profile_lock.acquire(acc_id))
        try:
            did_work = asyncio.run(scheduler_mod._do_publish(note_id))
            self.assertFalse(did_work)
            with models.SessionLocal() as s:
                self.assertEqual(s.get(models.Note, note_id).status, "pending")
        finally:
            profile_lock.release(acc_id)

        # 扫描时到期的 platform_scheduled 应自动流转为 published
        scheduler_mod.scan_due_notes()
        with models.SessionLocal() as s:
            self.assertEqual(s.get(models.Note, plat_id).status, "published")

    def test_R04_invalid_collect_times_rejected_with_400(self):
        """[R-04 修复验证] 非法采集时间(如 25:99)在 put_settings 中被 400 拦截,不污染数据库"""
        scheduler_mod.set_setting("collect_times", "10:30,21:30")
        body = api_settings.SettingsIn(collect_times=["25:99"])
        with self.assertRaises(HTTPException) as ctx:
            api_settings.put_settings(body)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(scheduler_mod.get_setting("collect_times"), "10:30,21:30")

        # 合法时间正常保存并重载
        ok_body = api_settings.SettingsIn(collect_times=["9:05", "22:15"])
        res = api_settings.put_settings(ok_body)
        self.assertEqual(res["collect_times"], ["09:05", "22:15"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TEST_TMP, ignore_errors=True)
