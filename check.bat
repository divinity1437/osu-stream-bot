@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe app.py --check
) else (
    python app.py --check
)
