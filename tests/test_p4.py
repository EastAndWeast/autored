"""小红书 AI 运营助手 - P4 数据闭环测试套件。

涵盖:
- 数据库增量迁移 v3(账号采集追踪与快照来源字段)
- 创作者中心数据匹配策略: 平台ID优先精确匹配、单篇标题受控兜底
- 同名笔记歧义保护机制(Ambiguous Detection): 存在多篇同名笔记时坚决不自动乱绑定
- 区分「未采集(pending_first_fetch)」与「真实读数为0(verified_zero)」
- 国内版与海外版 Rednote 平台兼容状态清晰标记(不虚报全兼容)
- 采集结果快照、更新时间与不可用错误记录
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.config as config
from app import models
from app.migrations.runner import get_applied_versions
from app.scheduler import _match_note
from app.services.stats import (
    METRICS_DEFINITIONS,
    get_notes_stats_service,
    get_stats_overview_service,
)


class P4TestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="xhs_p4_test_"))
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
            s.query(models.NoteStat).delete()
            s.query(models.PublishAttempt).delete()
            s.query(models.PublishJob).delete()
            s.query(models.Note).delete()
            s.query(models.Account).delete()
            s.commit()

        # 创建一个国内账号和一个海外账号
        with models.SessionLocal() as s:
            acc_cn = models.Account(
                id=1,
                name="国内美妆号",
                status="online",
                platform_region="cn",
                creator_base="https://creator.xiaohongshu.com",
            )
            acc_global = models.Account(
                id=2,
                name="海外穿搭号",
                status="online",
                platform_region="global",
                creator_base="https://creator.rednote.com",
            )
            s.add_all([acc_cn, acc_global])
            s.commit()

    # ==================== 1. 数据库增量迁移 v3 ====================

    def test_migration_003_applied(self):
        applied = get_applied_versions(self.db_path)
        self.assertIn(1, applied)
        self.assertIn(2, applied)
        self.assertIn(3, applied)

    # ==================== 2. 数据匹配策略与歧义保护 ====================

    def test_stats_matching_platform_id_first(self):
        """场景 A: 平台 ID 精确匹配(最高优先级)。"""
        with models.SessionLocal() as s:
            note = models.Note(
                account_id=1,
                title="本地草稿原标题",
                content="内容",
                status="published",
                xhs_note_id="platform_666_abc",
                published_at=datetime.now(),
            )
            s.add(note)
            s.commit()
            target_id = note.id

        with models.SessionLocal() as s:
            row = {
                "note_id": "platform_666_abc",
                "title": "平台可能微调过的展示标题",
                "impression": 5000,
                "read": 1200,
            }
            matched_note, matched_by = _match_note(s, account_id=1, row=row)
            self.assertIsNotNone(matched_note)
            self.assertEqual(matched_note.id, target_id)
            self.assertEqual(matched_by, "platform_id")

    def test_stats_matching_title_fallback(self):
        """场景 B: 仅有唯一单篇同名笔记时, 允许标题受控兜底并回填平台 ID。"""
        with models.SessionLocal() as s:
            note = models.Note(
                account_id=1,
                title="独一无二的咖啡探店",
                content="内容",
                status="published",
                xhs_note_id=None,  # 尚无平台 ID
                published_at=datetime.now(),
            )
            s.add(note)
            s.commit()
            target_id = note.id

        with models.SessionLocal() as s:
            row = {
                "note_id": "platform_new_id_888",
                "title": "独一无二的咖啡探店",
                "impression": 300,
                "read": 80,
            }
            matched_note, matched_by = _match_note(s, account_id=1, row=row)
            self.assertIsNotNone(matched_note)
            self.assertEqual(matched_note.id, target_id)
            self.assertEqual(matched_by, "title")
            # 验证回填了平台 ID
            self.assertEqual(matched_note.xhs_note_id, "platform_new_id_888")

    def test_stats_matching_ambiguous_title_rejected(self):
        """场景 C: 账号下存在多篇同名笔记时, 判定为歧义(ambiguous), 严禁盲目绑定!"""
        with models.SessionLocal() as s:
            note1 = models.Note(
                account_id=1,
                title="重名笔记标题",
                content="第一篇",
                status="published",
                xhs_note_id=None,
                published_at=datetime.now() - timedelta(days=2),
            )
            note2 = models.Note(
                account_id=1,
                title="重名笔记标题",
                content="第二篇",
                status="published",
                xhs_note_id=None,
                published_at=datetime.now() - timedelta(days=1),
            )
            s.add_all([note1, note2])
            s.commit()

        with models.SessionLocal() as s:
            row = {
                "note_id": "platform_xyz_999",
                "title": "重名笔记标题",
                "impression": 9999,
                "read": 2000,
            }
            matched_note, matched_by = _match_note(s, account_id=1, row=row)
            # 核心断言: 识别歧义, 放弃绑定, 绝不张冠李戴!
            self.assertIsNone(matched_note)
            self.assertEqual(matched_by, "ambiguous")

    # ==================== 3. 真实 0 与未采集状态区分 ====================

    def test_stats_status_distinction_pending_vs_zero_vs_tracked(self):
        """验证区分: 1. 待首次采集 2. 已采集但平台读数为 0 3. 正常追踪有指标。"""
        now = datetime.now()
        with models.SessionLocal() as s:
            # 笔记 1: 刚刚发布, 尚未有采集快照
            n1 = models.Note(
                id=10,
                account_id=1,
                title="刚发布的全新笔记",
                status="published",
                published_at=now,
            )
            # 笔记 2: 已执行采集, 但平台所有指标均为 0 (新帖或零曝光)
            n2 = models.Note(
                id=20,
                account_id=1,
                title="已采集但平台全为0的笔记",
                status="published",
                published_at=now - timedelta(hours=5),
            )
            stat2 = models.NoteStat(
                note_id=20,
                fetched_at=now,
                impression=0,
                read=0,
                like=0,
                collect=0,
                source="dom",
            )
            # 笔记 3: 正常有读数
            n3 = models.Note(
                id=30,
                account_id=1,
                title="有热度正常追踪的笔记",
                status="published",
                published_at=now - timedelta(days=1),
            )
            stat3 = models.NoteStat(
                note_id=30,
                fetched_at=now,
                impression=8000,
                read=2300,
                like=150,
                collect=80,
                source="api",
            )
            s.add_all([n1, n2, stat2, n3, stat3])
            s.commit()

        notes_data = get_notes_stats_service(account_id=1)
        data_by_id = {item["note_id"]: item for item in notes_data}

        # 检查笔记 1: 待首次采集
        self.assertEqual(data_by_id[10]["tracking_status"], "pending_first_fetch")
        self.assertIn("未采集", data_by_id[10]["tracking_status_text"])
        self.assertIsNone(data_by_id[10]["fetched_at"])

        # 检查笔记 2: 真实读数为 0
        self.assertEqual(data_by_id[20]["tracking_status"], "verified_zero")
        self.assertIn("读数为 0", data_by_id[20]["tracking_status_text"])
        self.assertEqual(data_by_id[20]["read"], 0)
        self.assertIsNotNone(data_by_id[20]["fetched_at"])

        # 检查笔记 3: 正常追踪
        self.assertEqual(data_by_id[30]["tracking_status"], "tracked")
        self.assertEqual(data_by_id[30]["read"], 2300)
        self.assertEqual(data_by_id[30]["source"], "api")

    # ==================== 4. 双平台兼容性标记 ====================

    def test_dual_platform_compatibility_tagging(self):
        """交接约定: 双平台未验证的一侧清楚标记, 不虚报全兼容。"""
        overview = get_stats_overview_service()
        overview_by_id = {a["account_id"]: a for a in overview}

        # 国内版账号: fully_supported
        acc1 = overview_by_id[1]
        self.assertEqual(acc1["platform_region"], "cn")
        self.assertEqual(acc1["compatibility"], "fully_supported")
        self.assertIn("完整验证", acc1["compatibility_text"])

        # 海外版账号: experimental_unverified
        acc2 = overview_by_id[2]
        self.assertEqual(acc2["platform_region"], "global")
        self.assertEqual(acc2["compatibility"], "experimental_unverified")
        self.assertIn("海外版实验中", acc2["compatibility_text"])

    def test_metrics_definitions(self):
        """测试指标定义字典完整包含所有核心互动项。"""
        for k in ("impression", "read", "like", "collect", "comment", "share", "fans_gain"):
            self.assertIn(k, METRICS_DEFINITIONS)
            self.assertTrue(len(METRICS_DEFINITIONS[k]) > 5)

