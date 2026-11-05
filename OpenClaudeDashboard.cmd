REM SPDX-License-Identifier: MIT
@echo off
rem Runs OpenClaudeDashboard.ps1 even when the PowerShell execution policy blocks unsigned scripts.
rem Dashboard entry point for this checkout.
setlocal
set "CLAUDE_CHEAT_PS=powershell.exe"
where pwsh.exe >nul 2>nul && set "CLAUDE_CHEAT_PS=pwsh.exe"
"%CLAUDE_CHEAT_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0OpenClaudeDashboard.ps1" %*
exit /b %ERRORLEVEL%
