# Codex 宿主接入指南

小红书 AI 运营助手通过标准 MCP (Model Context Protocol) Stdio 协议与 Codex 深度集成。

## 一、前置条件

1. 本地运行助手服务或虚拟环境：
   - 项目目录：`<项目目录>`
   - Python 虚拟环境：`./venv/bin/python`
2. 宿主 Codex 支持 MCP 工具规范。

## 二、配置方式

### 方式 1：Codex 配置文件中添加

打开您的 Codex 配置文件（如 `~/.codex/config.json` 或项目级 `.codex/mcp.json`），在 `mcpServers` 节点中增加：

```json
{
  "mcpServers": {
    "xhs-ai-assistant": {
      "command": "<项目目录>/venv/bin/python",
      "args": [
        "-m",
        "app.mcp.server"
      ],
      "cwd": "<项目目录>",
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### 方式 2：加载 Skill 指令

将 `skills/codex/skill.md` 内容添加至 Codex 的 Custom Instructions 或 System Prompt 中，以让 AI 遵循标准的草稿保存与发布流程。

## 三、常用对话示例

1. **生成并存为草稿**：
   > “帮我给账号 1 创作一篇关于‘春季露营穿搭’的小红书，我桌面上有一张配图 `<桌面路径>/camping.jpg`。”
   > → AI 会自动调用 `import_media` 托管配图，生成爆款标题、正文与话题，调用 `create_draft` 保存为草稿并给出完整卡片预览。

2. **改写草稿**：
   > “把刚才那篇草稿的标题改得更有吸引力一点，正文再强调一下冲锋衣的防水性能。”
   > → AI 调用 `update_draft` 并带上当前 `expected_revision`，递增修订号。

3. **安排排期发布**：
   > “预览没问题，定在明天晚上 8 点发布吧。”
   > → AI 调用 `schedule_publish(mode='schedule', scheduled_at='2026-11-07 20:00')`，返回后台任务状态。

4. **查询状态与数据**：
   > “查一下我账号最近发布的所有笔记与数据表现。”
   > → AI 调用 `get_stats` 与 `list_notes` 展示阅读量、点赞收藏明细。

