@echo off
setlocal
chcp 65001 >nul
title SiYi Desktop Diagnostics
rem Diagnostic entry only. Use the sibling .vbs entry for normal no-console startup.
rem Do not inherit PowerShell 7 module paths into Windows PowerShell 5.1.
set "PSModulePath="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\start-local-desktop.ps1"
if errorlevel 1 (
  echo.
  echo SiYi did not start. No existing application was stopped.
  exit /b 1
)
