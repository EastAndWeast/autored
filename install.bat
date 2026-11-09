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
REM 支持范围与 packaging/RELEASE_MANIFEST.json 一致: >=3.9,<3.14(CI 已验证 3.13)
for /f %%n in ('python -c "import sys;print(sys.version_info.major*100+sys.version_info.minor)"') do set PYNUM=%%n
if %PYNUM% LSS 309 (
  echo [✗] Python %PYVER% 过旧,需要 3.9 ~ 3.12
  pause & exit /b 1
)
if %PYNUM% GEQ 314 (
  if not "%ALLOW_UNSUPPORTED_PYTHON%"=="1" (
    echo [✗] Python %PYVER% 不受支持(需要 3.9 ~ 3.13)。可安装 3.12/3.13,或先 set ALLOW_UNSUPPORTED_PYTHON=1 强制继续
    pause & exit /b 1
  )
  echo [!] Python %PYVER% 不在官方支持范围,已按 ALLOW_UNSUPPORTED_PYTHON=1 强制继续
)

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

REM ---------- 6.5 自动注册到 WorkBuddy(安全合并;已存在则不动) ----------
set "WBDir=%USERPROFILE%\.workbuddy"
set "WBCfg=%WBDir%\mcp.json"
if not exist "%WBDir%" mkdir "%WBDir%"
if exist "%WBCfg%" copy /y "%WBCfg%" "%WBCfg%.bak" >nul
set "RegScript=%TEMP%\wb_register.ps1"
> "%RegScript%" echo $cfg = "%WBCfg%"
>> "%RegScript%" echo $server = @{ command = "%VENV_PYTHON:\=\/%"; args = @("-m","app.mcp.server"); cwd = "%PROJECT_DIR:\=\/%"; env = @{ PYTHONUNBUFFERED = "1" } }
>> "%RegScript%" echo if (Test-Path $cfg) { $data = Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json } else { $data = [pscustomobject]@{ mcpServers = [pscustomobject]@{} } }
>> "%RegScript%" echo if ($data.mcpServers.PSObject.Properties['xhs-assistant']) { Write-Host "[i] WorkBuddy 配置中已存在 xhs-assistant,保持原样未覆盖" } else { if (-not $data.mcpServers) { $data ^| Add-Member -Force -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{}) }; $data.mcpServers ^| Add-Member -Force -MemberType NoteProperty -Name 'xhs-assistant' -Value ([pscustomobject]$server); $data ^| ConvertTo-Json -Depth 6 ^| Set-Content -Path $cfg -Encoding UTF8; Write-Host "[✓] 已注册到 $cfg(原文件备份为 .bak)" }
powershell -NoProfile -ExecutionPolicy Bypass -File "%RegScript%" >nul 2>nul && (echo [✓] WorkBuddy 自动注册完成) || (echo [!] 自动注册失败,请手动把 mcp.generated.json 的 mcpServers 段合并进 %WBCfg%)
del "%RegScript%" 2>nul
echo [i] 别忘了:在 WorkBuddy「连接器管理 → 自定义连接器」里对新出现的 xhs-assistant 点「信任」

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
