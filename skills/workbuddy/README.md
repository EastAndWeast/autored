# WorkBuddy 宿主接入指南

本目录包含 WorkBuddy 的连接器与技能定义包。

## 一、连接器安装

1. 打开 WorkBuddy 设置 -> 连接器管理（Connectors） -> 添加本地自定义连接器。
2. 选择本目录 `skills/workbuddy`，或将 `package.json` 和 `connector.json` 路径导入。
3. 确保运行环境 Python 路径指向虚拟环境 `./venv/bin/python`。

## 二、功能特性

- **多宿主共用常驻后台**：WorkBuddy 启动的 MCP Stdio 进程与管理网页、Codex 宿主共享同一个 SQLite 与受管素材库，不重复创建 Worker，不争抢浏览器。
- **草稿物理防误发**：所有 AI 创作必须落盘为草稿，只有在用户授权发布排期时才进入任务队列。
- **崩溃自动防重发**：提交平台中途断开时进入 `verifying` 核对模式，绝不盲目补发。

