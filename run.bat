@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe run.py
) else (
  echo 尚未安装,请先运行 install.bat
  pause
)
