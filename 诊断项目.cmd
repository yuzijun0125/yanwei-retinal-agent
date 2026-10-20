@echo off
chcp 65001 >nul

setlocal

cd /d "%~dp0"

if not exist "%~dp0启动完整项目.ps1" (

 echo Please extract the complete ZIP before running.

 pause

 exit /b 1

)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动完整项目.ps1" -Diagnose

if errorlevel 1 pause
