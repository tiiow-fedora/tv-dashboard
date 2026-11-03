@echo off
rem Starts the TV dashboard service detached (no console). Panel: http://127.0.0.1:8901/
cd /d "%~dp0"
start "" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" "%~dp0tvdash.py"
