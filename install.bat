@echo off
rem Wrapper: runs install.ps1 without changing the system execution policy.
rem Extra arguments are passed through, e.g.  install.bat -Source mesh-warp.zip
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
