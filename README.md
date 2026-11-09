# 小红书 AI 运营助手

**给 WorkBuddy(及其他 MCP 客户端)接上一双「小红书运营的手脚」**——本地运行的多账号发布与数据追踪服务。装好后,在 WorkBuddy 对话里就能:管理多账号、写草稿、定时发布、追踪每篇笔记数据。所有账号数据只存在你自己电脑上。

> 工作方式:WorkBuddy 通过 MCP 协议调用本工具;发布由真实浏览器模拟人工操作完成(patchright 反检测),你用自己的账号扫码登录,不经手任何第三方。

## 三步上手

**方式一:一句话安装(推荐)**

macOS / Linux 终端一条命令,自动克隆到 `~/autored` 并装好一切:

```bash
curl -fsSL https://raw.githubusercontent.com/EastAndWeast/autored/main/install.sh | bash
```

> 自定义位置:`curl -fsSL <同上URL> | XHS_HOME=/目标路径 bash`
> 国内 raw 慢的等价替代:`curl -fsSL https://cdn.jsdelivr.net/gh/EastAndWeast/autored@main/install.sh | bash`
> Windows(PowerShell):`git clone https://github.com/EastAndWeast/autored.git $env:USERPROFILE\autored; cd $env:USERPROFILE\autored; .\install.bat`

**方式二:在 WorkBuddy 里一句话安装(对话式)**

把这句话粘贴给 WorkBuddy:

> 帮我安装开源项目 https://github.com/EastAndWeast/autored (小红书 AI 运营助手):克隆到 ~/autored,运行其中的 install.sh 完成依赖安装,装好后把生成的 mcp.generated.json 注册到你的 MCP 配置里(若无法自动注册,告诉我手动导入的步骤),最后验证 tools/list 可以连通。

**方式三:手动克隆**

```bash
git clone https://github.com/EastAndWeast/autored.git
cd autored
bash install.sh            # macOS / Linux;Windows 双击 install.bat
```

装完在 WorkBuddy 里说「**查询助手状态**」验证连接,然后「**启动登录**」开始添加账号。

> 说明:克隆 + 安装 + 生成 MCP 配置全自动;最后「导入 mcp.generated.json 到 WorkBuddy」取决于你的 WorkBuddy 版本是否支持从文件自动注册——支持则零操作,不支持时在设置页手动导入一次(一分钟内)。

## 在 WorkBuddy 里怎么用

| 你说 | 助手做 |
|---|---|
| 「启动登录,账号叫美妆小号」 | 发起登录,提示你打开 http://127.0.0.1:8643 用小红书 APP 扫码 |
| 「写一篇关于秋日穿搭的笔记,存成草稿」 | 保存草稿并给出标准卡片预览(**默认只存草稿,绝不擅自发布**) |
| 「把 3 号笔记定今晚 8 点发」 | 校验要素后进入发布队列;≥2.5h 走平台官方定时,更近则到点自动发布 |
| 「看看最近笔记数据怎么样」 | 汇总每账号/每篇的曝光、阅读、点赞、收藏、评论、分享 |

**安全红线**:AI 默认只保存草稿;只有你明确说「发出去」「定在 x 点发」,才会调用发布。图片素材经受管导入(自动备份+魔数校验),并发修改有修订号保护。

## 功能

- **多账号管理**:每账号独立浏览器环境(cookie/指纹完全隔离),扫码登录,登录态自动检测
- **立即/定时发布**:定时 ≥2.5h → 小红书官方「定时发布」入平台队列;更近 → 本地到点唤醒,±10 分钟抖动
- **图文 + 视频**,话题、多图(≤18 张);发布任务防重(幂等键 + 修订号 + 提交中断转人工核对,绝不盲目重发)
- **数据追踪**:每账号每篇笔记的曝光/阅读/点赞/收藏/评论/分享/涨粉,每日定时采集 + 手动刷新,历史趋势
- **网页管理后台**:http://127.0.0.1:8643 (账号/笔记/数据/日志五个页签)
- **Obsidian 工作流**:一个 .md = 一篇笔记,frontmatter(title/tags/account/scheduled_at/cover),自动解析 `![[图片]]`,发布后移入「已发布」

## 手动安装(不用脚本)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt        # 国内加 -i https://pypi.tuna.tsinghua.edu.cn/simple
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" ./venv/bin/patchright install chromium
./venv/bin/python run.py                          # 打开 http://127.0.0.1:8643
```

> 需要 Python ≥3.9。换端口:`XHS_PORT=9000`。数据目录:`XHS_DATA_DIR`。更多环境变量见 `app/config.py` 注释。

## 使用流程(网页后台)

1. **账号管理** → 添加账号 → 小红书 APP 扫码并在手机确认 → 状态变「在线」
2. **笔记管理** → 新建笔记(选账号、标题正文、传图/视频、立即或定时)→ 自动进队列
3. **内容数据** → 每天自动采集 2 次(时间可在设置页改),也可手动刷新

### Obsidian 笔记格式

```markdown
---
title: 可选,默认取文件名/首个标题
tags: 穿搭, 秋冬
account: 账号备注名(可选)
scheduled_at: 2026-11-20 19:30
---

正文…(markdown 轻度转纯文本)

![[配图.png]]
```

## 防封机制(降低概率,不能消除)

| 机制 | 说明 |
|---|---|
| patchright | 反自动化检测的 Playwright 补丁版,修复 webdriver 等特征 |
| 环境隔离 | 每账号独立 user_data_dir + 固定 UA/视窗指纹 + 可选独立代理 |
| 扫码登录 | 不碰账密;登录态持久化,扫码后自动校验;会话 Cookie 本地保留,服务端可提前失效 |
| 串行错峰 | 全局一次只开一个自动化浏览器;任务间随机休眠 5~15 分钟 |
| 频控 | 每账号每日上限(默认 2),超限自动顺延次日 |
| 随机化 | 定时 ±10 分钟抖动;逐字输入、随机停顿、随机滚动 |

**建议**:小号先试水;新号先手动养几天再上自动化;控制频次;重要账号谨慎。

## 常见问题

- **WorkBuddy 连不上工具** → 确认服务在运行(浏览器能打开 http://127.0.0.1:8643);MCP 配置里 command/cwd 必须是你机器上的绝对路径(mcp.generated.json 已自动生成)
- **扫码后一直等待** → 在登录浏览器里完成手机确认/验证码;登录期间保持窗口打开
- **发布失败:登录态已失效** → 账号页点「重新扫码」
- **话题没加上** → 平台联想接口没返回候选会自动跳过,不影响发布
- **数据采集为 0 条** → 看「运行日志」定位;采集三重策略:同源 API → el-table 表头映射 → 固定列索引兜底(xhs-toolkit 实测列序)
- **数据文件位置**:数据库 `data/app.db`,浏览器环境 `data/profiles/`,媒体 `data/uploads/`

## 致谢

站在这些开源项目的肩膀上:

| 项目 | 借鉴 |
|---|---|
| [social-auto-upload](https://github.com/dreammis/social-auto-upload) | patchright 反检测、扫码登录流程、发布 selector、平台原生定时 |
| [xhs-toolkit](https://github.com/aki66938/xhs-toolkit) | 创作者中心数据采集、话题逐字符输入、上传轮询 |
| [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | 登录态页面内调 API 取数方法论、账号环境隔离思路 |
| [Spider_XHS](https://github.com/cv-cat/Spider_XHS) | 验证了创作者平台能力边界(未采用其逆向路线) |

## 免责声明

自动化操作小红书违反其用户协议,存在限流/封号风险。本项目按「现状」开源,仅供学习与个人内容管理;使用者须使用自己的账号并自行承担使用后果,请勿用于商业刷量或代运营等场景。
