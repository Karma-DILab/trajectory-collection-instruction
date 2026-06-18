@echo off
rem Web Tracker launcher: starts the local server and opens the manual page.
rem Double-click this file to run. Works from any current directory.
setlocal
cd /d "%~dp0"

rem Prefer the project venv; fall back to the system Python.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Web Tracker...
echo (A browser tab will open at http://localhost:5000/ shortly.)
echo Press Ctrl+C in this window to stop.
echo.

"%PY%" webtracker_launcher.py

echo.
echo Web Tracker stopped.
pause
