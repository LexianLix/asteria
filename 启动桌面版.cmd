@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m paper_reader desktop
) else (
  python -m paper_reader desktop
)
if errorlevel 1 pause
