@echo off
REM ===================================================================
REM  Ceonggo Setup - run from source (developers). Users run "Ceonggo Setup.exe".
REM ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Ceonggo Setup

REM The project's own .venv wins when it exists, so running from source never
REM installs anything into the system Python. Without one, fall back to the
REM Python on PATH and install the two window libraries there, as before.
set "PY="
set "PYW="
set "VENV=%~dp0.venv\Scripts"
if exist "%VENV%\python.exe" set "PY=%VENV%\python.exe"
if exist "%VENV%\pythonw.exe" set "PYW=%VENV%\pythonw.exe"

if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python

"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :no_python

"%PY%" -c "import PySide6, requests" >nul 2>nul
if errorlevel 1 (
    echo Installing the window libraries once...
    "%PY%" -m pip install -r "%~dp0requirements.txt" || goto :failed
)

if not defined PYW where pyw >nul 2>nul && set "PYW=pyw"
if not defined PYW set "PYW=%PY%"
start "Ceonggo Setup" "%PYW%" "%~dp0CeonggoSetup.py"
exit /b 0

:no_python
echo Python 3.10 or newer is needed to run Ceonggo Setup from source.
echo Most people should use "Ceonggo Setup.exe" from the release zip instead.
pause
exit /b 1

:failed
echo Could not install the libraries. Try:  %PY% -m pip install -r requirements.txt
pause
exit /b 1
