@echo off
rem Runs OpenClaude.ps1 even when the PowerShell execution policy blocks unsigned scripts.
rem Routing session entry point for this checkout.
setlocal
set "CLAUDE_CHEAT_PS=powershell.exe"
where pwsh.exe >nul 2>nul && set "CLAUDE_CHEAT_PS=pwsh.exe"
"%CLAUDE_CHEAT_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0OpenClaude.ps1" %*
exit /b %ERRORLEVEL%
