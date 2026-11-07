# AI 宿主接入包与技能库 (Skills & MCP)

本目录提供主流 AI 宿主与工具（Codex、WorkBuddy、Claude Desktop、Cursor 等）接入「小红书/Rednote AI 运营助手」的标准规范与配置模板。

## 目录结构

```text
skills/
├── README.md               # 本文档
├── codex/                  # Codex 专用配置与 Skill
│   ├── config.json         # MCP 服务配置片段
│   ├── skill.md            # Codex 创作与排期行为规范
│   └── README.md           # Codex 接入使用说明
└── workbuddy/              # WorkBuddy 专用插件包
    ├── package.json        # WorkBuddy 连接器元信息
    ├── connector.json      # WorkBuddy MCP 运行配置
    ├── INSTRUCTION.md      # WorkBuddy AI 指令与人设
    └── README.md           # WorkBuddy 接入使用说明
```

## 核心设计准则

1. **草稿默认落盘**：AI 工具的所有创作（标题、正文、配图、话题）调用 `create_draft` 严格保存为 `draft`，绝不自动触发发布。
2. **账号严格隔离**：每次操作显式指定 `account_id`，国内版（`xiaohongshu.com`）与海外版（`rednote.com`）物理隔离。
3. **素材受管目录**：调用 `import_media` 将外部或生图临时文件复制进助手受管目录，防止聊天关闭后素材丢失。
4. **排期不阻塞请求**：调用 `schedule_publish` 仅将发布任务入库，后台常驻 worker 异步调度，MCP 请求毫秒级返回任务 ID 与状态。
5. **任务防重与自愈**：通过 `idempotency_key` 幂等防重，提交阶段异常或崩溃重启时强制转为 `verifying`（待核对），绝不盲目重发。

