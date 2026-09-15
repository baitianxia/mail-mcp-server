@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
set "UNINSTALL_SCRIPT=%~dp0scripts\uninstall.ps1"

if not exist "%UNINSTALL_SCRIPT%" (
  echo Uninstall files are incomplete: scripts\uninstall.ps1 was not found.
  pause
  exit /b 2
)

cd /d "%~dp0"
set "MAIL_ROOT=%USERPROFILE%\mail-mcp-server"
set "MAIL_LOG_DIR=%MAIL_ROOT%\logs"
set "MAIL_LAUNCH_LOG=%MAIL_LOG_DIR%\UNINSTALL-%RANDOM%-%RANDOM%.log"
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%UNINSTALL_SCRIPT%" -LogPath "%MAIL_LAUNCH_LOG%"
set "UNINSTALL_EXIT=%ERRORLEVEL%"

echo.
if "%UNINSTALL_EXIT%"=="0" (
  echo 邮件助手已从 Claude 用户级 MCP 中移除。
) else (
  echo Uninstall stopped with exit code %UNINSTALL_EXIT%.
  if exist "%MAIL_LAUNCH_LOG%" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath $env:MAIL_LAUNCH_LOG -Tail 40"
)
echo 诊断日志：%MAIL_LAUNCH_LOG%
pause
exit /b %UNINSTALL_EXIT%
