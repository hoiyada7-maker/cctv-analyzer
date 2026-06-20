@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8

set PYTHON=C:\Users\mccha\AppData\Local\Programs\Python\Python312\python.exe
set FFMPEG_BIN=C:\Users\mccha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin

set PATH=%FFMPEG_BIN%;C:\Users\mccha\AppData\Local\Programs\Python\Python312;C:\Users\mccha\AppData\Local\Programs\Python\Python312\Scripts;C:\Windows\System32;C:\Windows

cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo [%date% %time%] START >> "%~dp0logs\scheduler.log"
"%PYTHON%" main.py >> "%~dp0logs\scheduler.log" 2>&1
echo [%date% %time%] END (exit %errorlevel%) >> "%~dp0logs\scheduler.log"
echo. >> "%~dp0logs\scheduler.log"
