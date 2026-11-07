"""小红书 AI 运营助手 - P5 安装与发行测试套件。

涵盖:
- 操作系统标准数据目录解析规范与便携模式兼容
- macOS LaunchAgent 用户级开机自启服务配置生成
- SQLite 原生在线一致性备份、校验清单(manifest.json)生成
- 安全数据回退恢复与高版本 Schema 危险降级拦截
- Codex MCP 配置文件安全合并(保护用户已有的其它第三方 MCP 服务不被覆盖)
- WorkBuddy 连接器配置更新
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import app.config as config
from app import models
from app.services.backup import create_backup, list_backups, restore_backup
from app.services.errors import ServiceError
from app.services.host_config import configure_codex, configure_workbuddy
from app.services.launchagent import generate_plist_dict, generate_plist_xml


class P5TestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="xhs_p5_test_"))
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

    # ==================== 1. 数据目录解析规范 ====================

    def test_resolve_default_data_dir_env_priority(self):
        """验证环境变量 XHS_DATA_DIR 具备最高优先级。"""
        fake_dir = self.test_dir / "custom_env_data"
        old_env = os.environ.get("XHS_DATA_DIR")
        try:
            os.environ["XHS_DATA_DIR"] = str(fake_dir)
            resolved = config.resolve_default_data_dir()
            self.assertEqual(resolved, fake_dir.resolve())
        finally:
            if old_env is not None:
                os.environ["XHS_DATA_DIR"] = old_env
            else:
                os.environ.pop("XHS_DATA_DIR", None)

    # ==================== 2. macOS LaunchAgent 配置生成 ====================

    def test_launchagent_plist_generation(self):
        """测试生成的 LaunchAgent 结构符合 macOS launchd 规范。"""
        plist_d = generate_plist_dict()
        self.assertEqual(plist_d["Label"], "com.xhs.assistant")
        self.assertTrue(plist_d["RunAtLoad"])
        self.assertIn("ProgramArguments", plist_d)
        self.assertIn("_serve", plist_d["ProgramArguments"])
        self.assertIn("StandardOutPath", plist_d)
        self.assertIn("StandardErrorPath", plist_d)

        # 检查 XML 格式
        xml_str = generate_plist_xml()
        self.assertTrue(xml_str.startswith("<?xml"))
        self.assertIn("<plist", xml_str)
        self.assertIn("com.xhs.assistant", xml_str)

    # ==================== 3. 数据备份与安全回退 ====================

    def test_backup_and_restore_cycle(self):
        """测试全量数据快照备份与安全恢复。"""
        # 1. 在沙箱库塞入一条测试数据
        with models.SessionLocal() as s:
            acc = models.Account(id=100, name="备份测试账号", status="online")
            note = models.Note(id=200, account_id=100, title="待备份笔记", status="draft")
            s.add_all([acc, note])
            s.commit()

        # 生成一个素材文件
        test_file = config.UPLOADS_DIR / "backup_test.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        # 2. 执行备份
        res = create_backup(tag="unit_test")
        backup_dir = Path(res["backup_path"])
        self.assertTrue(backup_dir.exists())
        self.assertTrue((backup_dir / "app.db").exists())
        self.assertTrue((backup_dir / "manifest.json").exists())

        # 验证 manifest
        manifest = res["manifest"]
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertIn(3, manifest["schema_versions"])
        self.assertTrue(len(manifest["db_sha256"]) == 64)

        # 3. 模拟数据被意外修改/删除
        with models.SessionLocal() as s:
            n = s.get(models.Note, 200)
            if n:
                s.delete(n)
                s.commit()
            self.assertIsNone(s.get(models.Note, 200))

        # 4. 从备份恢复
        restore_res = restore_backup(backup_dir)
        self.assertTrue(restore_res["ok"])

        # 验证数据成功恢复
        with models.SessionLocal() as s:
            recovered_note = s.get(models.Note, 200)
            self.assertIsNotNone(recovered_note)
            self.assertEqual(recovered_note.title, "待备份笔记")

    def test_restore_rejects_incompatible_higher_schema(self):
        """安全保护: 尝试恢复一个来自未来高版本 Schema(如 99)的快照时, 必须报错拒绝降级。"""
        fake_backup_dir = self.test_dir / "backups" / "snapshot_future"
        fake_backup_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": "0.9.0",
            "max_schema": 99,  # 当前只支持到 3
            "db_sha256": "fake",
        }
        (fake_backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ServiceError) as ctx:
            restore_backup(fake_backup_dir)
        self.assertEqual(ctx.exception.code, "SCHEMA_INCOMPATIBLE")

    # ==================== 4. Codex MCP 配置安全合并 ====================

    def test_configure_codex_merges_safely(self):
        """核心安全验证: 修改 Codex 配置时必须备份, 且完整保留用户已有其它 MCP 服务!"""
        fake_codex_file = self.test_dir / "fake_codex" / "config.json"
        fake_codex_file.parent.mkdir(parents=True, exist_ok=True)

        # 预设用户已有的其他 MCP 配置
        initial_config = {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                },
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                },
            }
        }
        fake_codex_file.write_text(json.dumps(initial_config, indent=2), encoding="utf-8")

        # 执行配置注入
        res = configure_codex(target_path=fake_codex_file)
        self.assertTrue(res["ok"])
        self.assertTrue(res["backup_created"])

        # 验证备份文件确实存在且内容未被改动
        bak_file = fake_codex_file.with_suffix(".json.bak")
        self.assertTrue(bak_file.exists())
        bak_data = json.loads(bak_file.read_text(encoding="utf-8"))
        self.assertIn("github", bak_data["mcpServers"])
        self.assertNotIn("xhs-ai-assistant", bak_data["mcpServers"])

        # 验证新配置文件完整合并了三者
        new_data = json.loads(fake_codex_file.read_text(encoding="utf-8"))
        all_servers = new_data["mcpServers"]
        self.assertIn("github", all_servers, "原有的 github MCP 不能丢失!")
        self.assertIn("filesystem", all_servers, "原有的 filesystem MCP 不能丢失!")
        self.assertIn("xhs-ai-assistant", all_servers, "xhs-ai-assistant 必须成功注入!")

    def test_configure_workbuddy(self):
        """测试 WorkBuddy 连接器配置就绪。"""
        res = configure_workbuddy()
        self.assertTrue(res["ok"])
        self.assertIn("workbuddy", res["connector_dir"])
