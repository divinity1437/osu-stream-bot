@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
)

set "RUN_MODE_VAL=%RUN_MODE%"
if "%RUN_MODE_VAL%"=="" (
    if exist ".env" (
        for /f "tokens=1,2 delims==" %%A in (.env) do (
            if /i "%%A"=="RUN_MODE" set "RUN_MODE_VAL=%%B"
        )
    )
)

if /i "%RUN_MODE_VAL%"=="render-worker" (
    venv\Scripts\python.exe render_worker.py %*
) else (
    venv\Scripts\python.exe app.py %*
)
