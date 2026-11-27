@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%PY_EXE%" goto run_python

set "PY_EXE=%LOCALAPPDATA%\Python\bin\python.exe"
if exist "%PY_EXE%" goto run_python

where py >nul 2>&1
if errorlevel 1 (
    set "PY_EXE=python.exe"
    goto run_python
)

py -3 main.py
goto done

:run_python
"%PY_EXE%" main.py

:done
set "RUN_EXIT=%ERRORLEVEL%"
if not "%RUN_EXIT%"=="0" pause
exit /b %RUN_EXIT%
