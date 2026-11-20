@echo off
cd /d "%~dp0"
echo Updating videos and research papers...
py pipeline.py
echo.
echo Starting your site...
start "GogginsTube server" py -m http.server 8000 --directory site
timeout /t 2 >nul
start http://localhost:8000
echo Done. Close the server window when you're finished.
pause
