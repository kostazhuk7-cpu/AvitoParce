@echo off
cd /d "%~dp0"
start "Avito Parser" python web_app.py
timeout /t 4 /nobreak >nul
start http://localhost:8765
echo Server: http://localhost:8765
echo Close this window to stop the server.
pause
