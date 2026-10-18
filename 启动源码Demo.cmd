@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 请先安装 Python 3.12，再按 README 安装 requirements-demo.txt。
  pause
  exit /b 1
)
if not exist "%~dp002_代码un_demo.py" (
  echo 请先完整解压或克隆仓库，不要在压缩包内直接运行。
  pause
  exit /b 1
)
python -X utf8 "02_代码un_demo.py" --port 8765
if errorlevel 1 pause
