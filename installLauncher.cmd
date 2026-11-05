REM SPDX-License-Identifier: MIT
@echo off
rem Runs the PowerShell installer even when the current PowerShell session blocks unsigned scripts.
rem Usage: installLauncher.cmd [-Remove] [-Target <dir>]
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installLauncher.ps1" %*
exit /b %ERRORLEVEL%
