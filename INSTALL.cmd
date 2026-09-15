@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "INSTALL_SCRIPT=%~dp0scripts\install.ps1"
if not exist "%INSTALL_SCRIPT%" (
  echo 安装文件不完整：找不到安装脚本。
  pause
  exit /b 2
)

echo 邮件助手安装/升级正在启动...
set "MAIL_ROOT=%USERPROFILE%\mail-mcp-server"
set "MAIL_LOG_DIR=%MAIL_ROOT%\logs"
set "MAIL_LAUNCH_LOG=%MAIL_LOG_DIR%\INSTALL-%RANDOM%-%RANDOM%.log"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_SCRIPT%" -LogPath "%MAIL_LAUNCH_LOG%"
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if "%INSTALL_EXIT%"=="0" (
  echo 邮件助手安装/升级已完成。
) else (
  echo Setup stopped with exit code %INSTALL_EXIT%.
  echo 请阅读上面的错误信息；现有配置和版本目录未删除。
  if exist "%MAIL_LAUNCH_LOG%" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath $env:MAIL_LAUNCH_LOG -Tail 40"
)
echo 诊断日志：%MAIL_LAUNCH_LOG%
pause
exit /b %INSTALL_EXIT%
