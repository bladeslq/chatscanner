@echo off
cd /d "%~dp0"
echo Installing dependencies...
py -m pip install -r requirements.txt
echo.
echo Done! Fill in .env and run start.bat
pause
