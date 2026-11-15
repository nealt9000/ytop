@echo off
cd /d "%~dp0"
title YTOP - close this window to stop
if not exist ".ytop\runtime\windows-x86_64\python\python.exe" (
    echo This YTOP stick has no runtime for windows-x86_64. Make the stick again with that platform included.
    pause
    exit /b 3
)
".ytop\runtime\windows-x86_64\python\python.exe" -s -E ".ytop\boot.py"
if errorlevel 1 pause
