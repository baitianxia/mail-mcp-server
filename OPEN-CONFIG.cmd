@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
set "MAIL_CONFIG_DIR=%USERPROFILE%\mail-mcp-server\config"
start "邮件助手配置" explorer.exe "%MAIL_CONFIG_DIR%"
exit /b 0
