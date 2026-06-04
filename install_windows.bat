@echo off
setlocal
cd /d "%~dp0"
python -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Installation terminée. Tu peux lancer run_bot.bat
pause
