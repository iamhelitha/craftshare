@echo off
title Minecraft Server Manager
cd /d "%~dp0"
echo %~dp0 | findstr /i /c:"Temp1_" /c:"\\Temp\\" >nul && (
  echo This is running from inside the zip. Your settings and world would be lost.
  echo Close this window, right-click the zip, choose "Extract All", then run start.bat
  echo from the extracted folder.
  pause
  exit /b 1
)
where python >nul 2>nul || (
  echo Python is not installed. Install it with:  winget install Python.Python.3.13
  echo Then close this window and run start.bat again.
  pause
  exit /b 1
)
if not exist .venv python -m venv .venv
.venv\Scripts\python -c "import aiohttp" 2>nul || (
  echo Installing what the manager needs, one time only...
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python app.py
if errorlevel 1 pause
