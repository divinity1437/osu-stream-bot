@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VERSION=0.12.1"
set "DANSER_DIR=tools\danser"
set "ARCHIVE=danser-%VERSION%-win.zip"
set "URL=https://github.com/Wieku/danser-go/releases/download/%VERSION%/%ARCHIVE%"

echo Installing danser-go %VERSION% for Windows...

if not exist "tools" mkdir tools
if not exist "%DANSER_DIR%" mkdir "%DANSER_DIR%"

if not exist "%ARCHIVE%" (
    echo Downloading %URL%...
    curl.exe -L -f -o "%ARCHIVE%" "%URL%"
    if errorlevel 1 (
        echo Failed to download %ARCHIVE%. Check internet connection or proxy settings.
        exit /b 1
    )
)

echo Extracting %ARCHIVE% into %DANSER_DIR%...
powershell -NoProfile -Command "Expand-Archive -Path '%ARCHIVE%' -DestinationPath '%DANSER_DIR%' -Force"
if errorlevel 1 (
    echo Failed to extract %ARCHIVE%.
    exit /b 1
)

if not exist "%DANSER_DIR%\settings" mkdir "%DANSER_DIR%\settings"
if not exist "%DANSER_DIR%\songs" mkdir "%DANSER_DIR%\songs"
if not exist "%DANSER_DIR%\skins" mkdir "%DANSER_DIR%\skins"
if not exist "%DANSER_DIR%\replays" mkdir "%DANSER_DIR%\replays"
if not exist "%DANSER_DIR%\videos" mkdir "%DANSER_DIR%\videos"

copy /Y danser-stream.json "%DANSER_DIR%\settings\stream.json" >nul
copy /Y danser-stream-cpu.json "%DANSER_DIR%\settings\stream-cpu.json" >nul
copy /Y danser-stream-amf.json "%DANSER_DIR%\settings\stream-amf.json" >nul

echo danser %VERSION% successfully installed in %DANSER_DIR%!
