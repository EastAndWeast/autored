"""小红书 AI 运营助手 - P2 MCP Stdio 适配器与工具协议测试套件。

测试 MCP JSON-RPC 2.0 协议分发、全部 14 个工具调用逻辑、端到端 AI 创作工作流、
并发乐观锁防覆盖、幂等去重防重复建发稿。
测试全部运行于隔离临时目录中,不污染生产数据。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.config as config
from app import models
from app.mcp.server import (
    TOOLS_REGISTRY,
    handle_tool_call,
    process_message,
)


class MCPTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="xhs_mcp_test_"))
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

        # 创建测试账号
        with models.SessionLocal() as s:
            acc = models.Account(
                id=1,
                name="AI测试账号",
                status="online",
                platform_region="cn",
                creator_base="https://creator.xiaohongshu.com",
            )
            s.add(acc)
            s.commit()

    def _create_sample_png(self, filename: str = "sample.png") -> Path:
        """在外部临时目录生成有效 PNG 签名图片。"""
        p = self.test_dir / "external_temp" / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        # PNG magic header
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        return p

    # ==================== 1. JSON-RPC 协议测试 ====================

    def test_jsonrpc_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        resp = process_message(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "xhs-ai-assistant")

    def test_jsonrpc_ping(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        resp = process_message(req)
        self.assertEqual(resp["id"], 2)
        self.assertEqual(resp["result"], {})

    def test_jsonrpc_notification(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = process_message(req)
        self.assertIsNone(resp)

    def test_jsonrpc_unknown_method(self):
        req = {"jsonrpc": "2.0", "id": 99, "method": "non_existent_method"}
        resp = process_message(req)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_list(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        resp = process_message(req)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected_tools = [
            "list_accounts",
            "start_login",
            "get_login_task",
            "create_draft",
            "update_draft",
            "delete_draft",
            "import_media",
            "list_notes",
            "get_note",
            "schedule_publish",
            "get_publish_job",
            "cancel_publish_job",
            "get_stats",
            "get_runtime_status",
        ]
        for exp in expected_tools:
            self.assertIn(exp, tool_names, f"缺少必须的 MCP 工具: {exp}")

    # ==================== 2. 工具调用单元测试 ====================

    def test_tool_list_accounts(self):
        res = handle_tool_call("list_accounts", {"status": "all"})
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["accounts"][0]["name"], "AI测试账号")
        self.assertIn("next_action", res)

    def test_tool_create_draft_default_not_publish(self):
        """测试创建草稿: 默认 status=draft, 不入队发布, 具备清晰 next_action。"""
        res = handle_tool_call(
            "create_draft",
            {
                "account_id": 1,
                "title": "测试草稿标题",
                "content": "正文内容",
                "tags": ["测试", "AI"],
            },
        )
        self.assertNotIn("isError", res)
        self.assertEqual(res["status"], "draft")
        self.assertEqual(res["revision"], 1)
        self.assertFalse(res["publish_ready"])
        self.assertIn("missing_for_publish", res)
        self.assertIn("media_paths", res["missing_for_publish"])
        self.assertIn("next_action", res)

        # 验证数据库中确实为 draft, 且无任何发布任务入队
        with models.SessionLocal() as s:
            note = s.get(models.Note, res["note_id"])
            self.assertIsNotNone(note)
            self.assertEqual(note.status, "draft")
            jobs = s.query(models.PublishJob).all()
            self.assertEqual(len(jobs), 0)

    def test_tool_import_media_and_validation(self):
        """测试素材导入: 复制外部文件到受管目录并校验文件类型魔数。"""
        valid_png = self._create_sample_png("test_photo.png")
        res = handle_tool_call("import_media", {"file_path": str(valid_png)})
        self.assertNotIn("isError", res)
        self.assertTrue(res["media_id"].endswith(".png"))
        dest_file = config.UPLOADS_DIR / res["media_id"]
        self.assertTrue(dest_file.exists())

        # 测试导入伪造魔数文件应拦截
        fake_file = self.test_dir / "external_temp" / "fake.png"
        fake_file.write_bytes(b"NOT_A_REAL_PNG_FILE_HEADER")
        fake_res = handle_tool_call("import_media", {"file_path": str(fake_file)})
        self.assertTrue(fake_res.get("isError"))
        self.assertEqual(fake_res["error_code"], "INVALID_FILE_HEADER")

    def test_tool_update_draft_and_revision_lock(self):
        """测试更新草稿: 修订号自增与并发乐观锁控制。"""
        draft_res = handle_tool_call(
            "create_draft",
            {"account_id": 1, "title": "初稿标题", "content": "初稿正文"},
        )
        note_id = draft_res["note_id"]
        self.assertEqual(draft_res["revision"], 1)

        # 正确 revision 更新
        up_res = handle_tool_call(
            "update_draft",
            {
                "note_id": note_id,
                "title": "二稿优化标题",
                "expected_revision": 1,
            },
        )
        self.assertNotIn("isError", up_res)
        self.assertEqual(up_res["revision"], 2)

        # 错误 revision 冲突被拦截
        conflict_res = handle_tool_call(
            "update_draft",
            {
                "note_id": note_id,
                "title": "冲突修改",
                "expected_revision": 1,  # 数据库已是 2, 期望 1 必然冲突
            },
        )
        self.assertTrue(conflict_res.get("isError"))
        self.assertEqual(conflict_res["error_code"], "REVISION_CONFLICT")

    def test_tool_schedule_publish_and_idempotency(self):
        """测试安排发布: 校验要素、生成后台任务、幂等防重发。"""
        # 1. 准备有效草稿与素材
        img_path = self._create_sample_png("pic1.png")
        media_res = handle_tool_call("import_media", {"file_path": str(img_path)})
        draft = handle_tool_call(
            "create_draft",
            {
                "account_id": 1,
                "title": "完整合规笔记",
                "content": "正文内容充足",
                "media_ids": [media_res["media_id"]],
            },
        )
        note_id = draft["note_id"]

        # 2. 安排立即发布
        pub_res = handle_tool_call(
            "schedule_publish",
            {
                "note_id": note_id,
                "mode": "immediate",
                "idempotency_key": "req-key-001",
            },
        )
        self.assertNotIn("isError", pub_res)
        job_id = pub_res["job_id"]
        self.assertEqual(pub_res["status"], "queued")
        self.assertIn("next_action", pub_res)

        # 3. 相同 idempotency_key 再次提交 -> 幂等返回同一 job_id
        idem_res = handle_tool_call(
            "schedule_publish",
            {
                "note_id": note_id,
                "mode": "immediate",
                "idempotency_key": "req-key-001",
            },
        )
        self.assertNotIn("isError", idem_res)
        self.assertEqual(idem_res["job_id"], job_id)

        # 4. 查询任务详情
        job_detail = handle_tool_call("get_publish_job", {"job_id": job_id})
        self.assertEqual(job_detail["job_id"], job_id)
        self.assertEqual(job_detail["status"], "queued")

    def test_tool_cancel_publish_job(self):
        """测试取消任务: 任务取消且草稿安全回退为 draft 状态。"""
        img_path = self._create_sample_png("pic2.png")
        media_res = handle_tool_call("import_media", {"file_path": str(img_path)})
        draft = handle_tool_call(
            "create_draft",
            {
                "account_id": 1,
                "title": "可取消笔记",
                "content": "正文内容",
                "media_ids": [media_res["media_id"]],
            },
        )
        pub_res = handle_tool_call(
            "schedule_publish",
            {"note_id": draft["note_id"], "mode": "immediate"},
        )
        job_id = pub_res["job_id"]

        # 执行取消
        cancel_res = handle_tool_call("cancel_publish_job", {"job_id": job_id})
        self.assertNotIn("isError", cancel_res)
        self.assertEqual(cancel_res["status"], "cancelled")

        # 验证笔记回退为 draft
        note_res = handle_tool_call("get_note", {"note_id": draft["note_id"]})
        self.assertEqual(note_res["status"], "draft")

    def test_tool_get_stats_and_runtime(self):
        stats_res = handle_tool_call("get_stats", {})
        self.assertIn("accounts_overview", stats_res)
        self.assertIn("notes_performance", stats_res)

        runtime_res = handle_tool_call("get_runtime_status", {})
        self.assertEqual(runtime_res["name"], "小红书 AI 运营助手")
        self.assertEqual(runtime_res["version"], "0.2.0")
        self.assertEqual(runtime_res["accounts"]["online"], 1)

    # ==================== 3. 端到端 AI 创作工作流模拟 ====================

    def test_end_to_end_ai_workflow_via_jsonrpc(self):
        """模拟宿主 AI(Codex/WorkBuddy) 通过 JSON-RPC 2.0 完成端到端创作流程:
        1. tools/call list_accounts: 选定可用账号
        2. tools/call import_media: 导入外部生成的素材
        3. tools/call create_draft: 保存为草稿
        4. tools/call update_draft: 改写优化正文
        5. tools/call schedule_publish: 定时发布
        6. tools/call get_publish_job: 轮询任务状态
        7. tools/call cancel_publish_job: 撤回排发并恢复草稿
        """
        # Step 1: 查询账号
        step1_req = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {"name": "list_accounts", "arguments": {}},
        }
        step1_resp = process_message(step1_req)
        step1_data = json.loads(step1_resp["result"]["content"][0]["text"])
        self.assertEqual(step1_data["count"], 1)
        target_account_id = step1_data["accounts"][0]["id"]
        self.assertEqual(target_account_id, 1)

        # Step 2: 导入生图素材
        sample_img = self._create_sample_png("ai_generated_image.png")
        step2_req = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {"name": "import_media", "arguments": {"file_path": str(sample_img)}},
        }
        step2_resp = process_message(step2_req)
        step2_data = json.loads(step2_resp["result"]["content"][0]["text"])
        media_id = step2_data["media_id"]

        # Step 3: 创建草稿 (初稿)
        step3_req = {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": "create_draft",
                "arguments": {
                    "account_id": target_account_id,
                    "title": "初夏海边穿搭指南",
                    "content": "阳光沙滩和海浪,分享三套超显白穿搭灵感~",
                    "tags": ["穿搭", "夏季穿搭", "海边"],
                    "media_ids": [media_id],
                },
            },
        }
        step3_resp = process_message(step3_req)
        step3_data = json.loads(step3_resp["result"]["content"][0]["text"])
        self.assertEqual(step3_data["status"], "draft")
        self.assertTrue(step3_data["publish_ready"])
        note_id = step3_data["note_id"]
        self.assertEqual(step3_data["revision"], 1)

        # Step 4: AI 按用户要求润色改写文案 (更新草稿)
        step4_req = {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": "update_draft",
                "arguments": {
                    "note_id": note_id,
                    "title": "美爆了!初夏海边穿搭灵感分享",
                    "content": "阳光沙滩与海浪,分享三套超显白上镜的夏日清爽穿搭灵感!喜欢的集美赶紧收藏起来~",
                    "expected_revision": 1,
                },
            },
        }
        step4_resp = process_message(step4_req)
        step4_data = json.loads(step4_resp["result"]["content"][0]["text"])
        self.assertEqual(step4_data["revision"], 2)

        # Step 5: 用户确认安排发布 (本地定时发布到 1 小时后)
        schedule_time_str = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        step5_req = {
            "jsonrpc": "2.0",
            "id": 105,
            "method": "tools/call",
            "params": {
                "name": "schedule_publish",
                "arguments": {
                    "note_id": note_id,
                    "mode": "schedule",
                    "scheduled_at": schedule_time_str,
                    "expected_revision": 2,
                    "idempotency_key": "ai-e2e-draft-001",
                },
            },
        }
        step5_resp = process_message(step5_req)
        step5_data = json.loads(step5_resp["result"]["content"][0]["text"])
        self.assertEqual(step5_data["status"], "scheduled")
        job_id = step5_data["job_id"]

        # Step 6: 轮询任务状态
        step6_req = {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {"name": "get_publish_job", "arguments": {"job_id": job_id}},
        }
        step6_resp = process_message(step6_req)
        step6_data = json.loads(step6_resp["result"]["content"][0]["text"])
        self.assertEqual(step6_data["status"], "scheduled")
        self.assertEqual(step6_data["note_status"], "scheduled")

        # Step 7: 用户改主意取消发布, 安全回退
        step7_req = {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {"name": "cancel_publish_job", "arguments": {"job_id": job_id}},
        }
        step7_resp = process_message(step7_req)
        step7_data = json.loads(step7_resp["result"]["content"][0]["text"])
        self.assertEqual(step7_data["status"], "cancelled")

        # 检查最终草稿状态回退为 draft
        step8_req = {
            "jsonrpc": "2.0",
            "id": 108,
            "method": "tools/call",
            "params": {"name": "get_note", "arguments": {"note_id": note_id}},
        }
        step8_resp = process_message(step8_req)
        step8_data = json.loads(step8_resp["result"]["content"][0]["text"])
        self.assertEqual(step8_data["status"], "draft")
