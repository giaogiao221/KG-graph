@echo off
REM 一键创建 demo 环境（Python 3.11 + 依赖）
REM 用法：在 demo 目录运行  setup_env.cmd
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 (
    echo [setup] 使用 uv 创建 .venv ^(Python 3.11^)...
    uv venv --python 3.11 --seed .venv
    uv pip install --python .venv\Scripts\python.exe -r requirements-lock.txt
) else (
    echo [setup] 未检测到 uv，使用系统 python 创建 .venv...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
)

echo.
echo [setup] 完成。验证：
.venv\Scripts\python.exe --version
endlocal
