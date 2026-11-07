@echo off
REM =============================================================
REM 小红书/Rednote AI 运营助手 - 一键安装脚本 (Windows)
REM 用法: 双击运行 或 install.bat
REM =============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set PROJECT_DIR=%cd%
set VENV_PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe

echo.
echo ==========================================
echo   小红书 AI 运营助手 · 一键安装
echo ==========================================
echo 项目目录: %PROJECT_DIR%
echo.

REM ---------- 1. Python ----------
where python >nul 2>nul
if errorlevel 1 (
  echo [✗] 未找到 python,请先安装 Python 3.9+ 并勾选 "Add to PATH"
  pause & exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo [✓] Python %PYVER%

REM ---------- 2. 虚拟环境 ----------
if not exist "%VENV_PYTHON%" (
  echo [!] 创建虚拟环境…
  python -m venv venv
)
echo [✓] 虚拟环境就绪

REM ---------- 3. pip 依赖 ----------
echo [!] 安装 Python 依赖,首次约 1-2 分钟…
"%VENV_PYTHON%" -m pip install --quiet --upgrade pip
"%VENV_PYTHON%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo [!] 官方源失败,改用清华镜像重试…
  "%VENV_PYTHON%" -m pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [✓] Python 依赖安装完成

REM ---------- 4. Chromium ----------
echo [!] 下载 Chromium,约 150MB…
"%VENV_PYTHON%" -m patchright install chromium
if errorlevel 1 (
  echo [!] 官方源失败,改用 npmmirror 镜像重试…
  set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
  "%VENV_PYTHON%" -m patchright install chromium
)
echo [✓] Chromium 就绪

REM ---------- 5. 数据目录 ----------
mkdir data\uploads 2>nul
mkdir data\profiles 2>nul
echo [✓] 数据目录就绪

REM ---------- 6. 生成 WorkBuddy connector 配置 ----------
set "TMPScript=%TEMP%\render_connector.ps1"
> "%TMPScript%" echo $p = Get-Content "skills\workbuddy\connector.template.json" -Raw
>> "%TMPScript%" echo $p = $p.Replace('{{VENV_PYTHON}}', '%VENV_PYTHON:\=\\%').Replace('{{PROJECT_DIR}}', '%PROJECT_DIR:\=\\%')
>> "%TMPScript%" echo Set-Content -Path "skills\workbuddy\connector.json" -Value $p -Encoding UTF8
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMPScript%" >nul 2>nul
del "%TMPScript%" 2>nul

> mcp.generated.json echo {
>> mcp.generated.json echo   "mcpServers": {
>> mcp.generated.json echo     "xhs-assistant": {
>> mcp.generated.json echo       "command": "%VENV_PYTHON:\=\\%",
>> mcp.generated.json echo       "args": ["-m", "app.mcp.server"],
>> mcp.generated.json echo       "cwd": "%PROJECT_DIR:\=\\%",
>> mcp.generated.json echo       "env": { "PYTHONUNBUFFERED": "1" }
>> mcp.generated.json echo     }
>> mcp.generated.json echo   }
>> mcp.generated.json echo }
echo [✓] 已生成 skills\workbuddy\connector.json 与 mcp.generated.json

REM ---------- 完成 ----------
echo.
echo ==========================================
echo 安装完成!
echo ==========================================
echo.
echo 下一步:
echo   1) 在 WorkBuddy 的 MCP 设置中添加,内容用 mcp.generated.json
echo      (或手动填 command=%VENV_PYTHON%  args=-m app.mcp.server  cwd=%PROJECT_DIR%)
echo   2) 在 WorkBuddy 对话里说「启动登录」,然后打开 http://127.0.0.1:8643 扫码
echo   3) 扫码成功后即可对话:写草稿 - 确认发布 - 查数据
echo.
echo 网页管理后台: 双击 run.bat 或运行 python run.py
echo.
pause
