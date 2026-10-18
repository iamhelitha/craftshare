@echo off
title Join the Minecraft server
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python is not installed. Install it with:  winget install Python.Python.3.13
  pause
  exit /b 1
)
python join.py
pause
