@echo off
REM =============================================================
REM 小红书/Rednote AI 运营助手 - 一键安装脚本 (Windows)
REM 用法: 双击运行 或 install.bat
REM 注意: 本文件内 echo 文本避免使用半角括号与竖线(cmd 转义陷阱)
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

REM ---------- 1. Python(优先用已有 venv,其次 PATH) ----------
set "PYPYTHON=python"
if exist "%VENV_PYTHON%" set "PYPYTHON=%VENV_PYTHON%"
where python >nul 2>nul
if errorlevel 1 if not exist "%VENV_PYTHON%" (
  echo [x] 未找到 python, 请先安装 Python 3.9 以上并勾选 Add to PATH
  pause & exit /b 1
)
for /f "tokens=2" %%i in ('%PYPYTHON% --version 2^>^&1') do set PYVER=%%i
for /f %%n in ('%PYPYTHON% -c "import sys;print(sys.version_info.major*100+sys.version_info.minor)"') do set PYNUM=%%n
REM 支持范围与 packaging/RELEASE_MANIFEST.json 一致: 3.9 <= 版本 < 3.14
if %PYNUM% GEQ 309 if %PYNUM% LSS 314 goto pyver_ok
if "%ALLOW_UNSUPPORTED_PYTHON%"=="1" (
  echo [!] Python %PYVER% 不在官方支持范围 3.9 到 3.13, 已按 ALLOW_UNSUPPORTED_PYTHON=1 强制继续
  goto pyver_ok
)
echo [x] Python %PYVER% 不受支持, 官方支持范围是 3.9 到 3.13
echo     请安装 3.12 或 3.13 后重试, 或先执行: set ALLOW_UNSUPPORTED_PYTHON=1
pause & exit /b 1
:pyver_ok
echo [OK] Python %PYVER%  [%PYPYTHON%]

REM ---------- 2. 虚拟环境 ----------
if not exist "%VENV_PYTHON%" (
  echo [..] 创建虚拟环境…
  "%PYPYTHON%" -m venv venv
)
echo [OK] 虚拟环境就绪

REM ---------- 3. pip 依赖 ----------
echo [..] 安装 Python 依赖, 首次约 1-2 分钟…
"%VENV_PYTHON%" -m pip install --quiet --upgrade pip
"%VENV_PYTHON%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo [!] 官方源失败, 改用清华镜像重试…
  "%VENV_PYTHON%" -m pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [OK] Python 依赖安装完成

REM ---------- 4. Chromium ----------
echo [..] 下载 Chromium, 约 150MB…
"%VENV_PYTHON%" -m patchright install chromium
if errorlevel 1 (
  echo [!] 官方源失败, 改用 npmmirror 镜像重试…
  set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
  "%VENV_PYTHON%" -m patchright install chromium
)
echo [OK] Chromium 就绪

REM ---------- 5. 数据目录 ----------
mkdir data\uploads 2>nul
mkdir data\profiles 2>nul
echo [OK] 数据目录就绪

REM ---------- 6. 生成 WorkBuddy connector 配置 ----------
"%VENV_PYTHON%" -c "import pathlib; p=pathlib.Path('skills/workbuddy/connector.template.json').read_text(encoding='utf-8'); import json; py=str(pathlib.Path(r'%VENV_PYTHON%')); proj=str(pathlib.Path(r'%PROJECT_DIR%')); p=p.replace('{{VENV_PYTHON}}', py).replace('{{PROJECT_DIR}}', proj); pathlib.Path('skills/workbuddy/connector.json').write_text(p, encoding='utf-8'); json.loads(p); print('OK connector.json rendered and valid')"
if errorlevel 1 (
  echo [x] connector.json 生成失败
  pause & exit /b 1
)
"%VENV_PYTHON%" -c "import json,sys; from pathlib import Path; py=str(Path(r'%VENV_PYTHON%')); proj=str(Path(r'%PROJECT_DIR%')); cfg={'mcpServers':{'xhs-assistant':{'command':py,'args':['-m','app.mcp.server'],'cwd':proj,'env':{'PYTHONUNBUFFERED':'1'}}}}; Path('mcp.generated.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8'); print('OK mcp.generated.json')"
if errorlevel 1 (
  echo [x] mcp.generated.json 生成失败
  pause & exit /b 1
)
echo [OK] 已生成 skills\workbuddy\connector.json 与 mcp.generated.json

REM ---------- 6.5 自动注册到 WorkBuddy(安全合并;已存在则不动) ----------
"%VENV_PYTHON%" "%PROJECT_DIR%\wb_register.py" "%PROJECT_DIR%"
if errorlevel 1 (
  echo [!] 自动注册未完成, 请手动把 mcp.generated.json 的 mcpServers 段合并进 %USERPROFILE%\.workbuddy\mcp.json
)
echo [i] 别忘了: 在 WorkBuddy「连接器管理 → 自定义连接器」里对新出现的 xhs-assistant 点「信任」

REM ---------- 完成 ----------
echo.
echo ==========================================
echo 安装完成!
echo ==========================================
echo.
echo 下一步:
echo   1) 在 WorkBuddy 的 MCP 设置中添加, 内容用 mcp.generated.json
echo      或手动填 command=%VENV_PYTHON%  args=-m app.mcp.server  cwd=%PROJECT_DIR%
echo   2) 在 WorkBuddy 对话里说「启动登录」, 然后打开 http://127.0.0.1:8643 扫码
echo   3) 扫码成功后即可对话: 写草稿 - 确认发布 - 查数据
echo.
echo 网页管理后台: 双击 run.bat 或运行 python run.py
echo.
if not defined CI pause
