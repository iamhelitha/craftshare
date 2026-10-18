@echo off
rem Builds dist\MinecraftManager.exe (no Python needed on the target PC), then packages it.
cd /d "%~dp0"
copy /y "..\..\VERSION" "VERSION" >nul
python -m pip install pyinstaller -r requirements.txt
python -m PyInstaller --onefile --name MinecraftManager --add-data "web;web" --add-data "VERSION;." --noconfirm app.py
python package.py --exe
