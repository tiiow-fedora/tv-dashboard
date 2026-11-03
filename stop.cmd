@echo off
rem Stops the TV dashboard service (the pythonw process running tvdash.py).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*tvdash.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
