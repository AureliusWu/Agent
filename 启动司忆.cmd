@echo off
chcp 65001 >nul
setlocal
set "APP=%~dp0司忆.exe"

if not exist "%APP%" (
  echo [司忆] 未找到最新桌面程序：%APP%
  echo 请先在项目根目录运行 scripts\build-runtime.ps1。
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-desktop.ps1" -ApplicationPath "%APP%"
