@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "CONFIGURE_SCRIPT=%~dp0scripts\configure-account.ps1"
if not exist "%CONFIGURE_SCRIPT%" (
  echo 邮件助手配置文件不完整：找不到配置脚本。
  pause
  exit /b 2
)

set "MAIL_ROOT=%USERPROFILE%\mail-mcp-server"
set "MAIL_LOG_DIR=%MAIL_ROOT%\logs"
set "MAIL_LAUNCH_LOG=%MAIL_LOG_DIR%\CONFIGURE-%RANDOM%-%RANDOM%.log"
echo 邮件助手配置向导正在启动...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CONFIGURE_SCRIPT%" -LogPath "%MAIL_LAUNCH_LOG%"
set "CONFIGURE_EXIT=%ERRORLEVEL%"

echo.
if "%CONFIGURE_EXIT%"=="0" (
  echo 邮件助手配置已完成。配置文件：%MAIL_ROOT%\config\settings.json
) else (
  echo 配置已停止，退出码 %CONFIGURE_EXIT%。
  if exist "%MAIL_LAUNCH_LOG%" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath $env:MAIL_LAUNCH_LOG -Tail 40"
)
echo 诊断日志：%MAIL_LAUNCH_LOG%
pause
exit /b %CONFIGURE_EXIT%
