"""小红书 AI 运营助手 - 运行管理入口脚本。

用法:
  python run.py                     # 默认前台启动服务
  python run.py start -d            # 后台守护进程启动
  python run.py stop                # 停止后台服务
  python run.py status              # 查看运行状态与队列
  python run.py doctor              # 运行全面环境健康诊断
  python run.py open                # 在浏览器中打开管理端网页
  python run.py service install     # 安装 macOS LaunchAgent 开机/登录常驻服务
  python run.py service uninstall   # 卸载开机常驻服务
  python run.py backup create       # 创建当前数据库与素材全量一致性快照
  python run.py backup list         # 查看所有历史备份快照
  python run.py backup restore <名> # 安全恢复指定备份点(带防降级与回滚快照)
  python run.py configure codex     # 安全合并 MCP 工具到 Codex 配置中
  python run.py configure workbuddy # 部署/更新 WorkBuddy 连接器
"""
import sys
from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
