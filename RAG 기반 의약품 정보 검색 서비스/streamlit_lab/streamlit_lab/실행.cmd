@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv_drug\Scripts\python.exe" (
  echo Python environment not found: .venv_drug
  pause
  exit /b 1
)
"%~dp0.venv_drug\Scripts\python.exe" "%~dp0launch_app.py" %*
if errorlevel 1 pause
endlocal
