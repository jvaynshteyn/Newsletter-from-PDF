@echo off
setlocal
cd /d "%~dp0"
title Newsletter to Email

if exist ".venv\Scripts\python.exe" goto use_venv
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 goto use_python
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" goto use_bundled
echo Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/ and select Add Python to PATH.
echo Then double-click this file again.
pause
exit /b 1

:use_venv
set "NEWSLETTER_PYTHON=.venv\Scripts\python.exe"
set "NEWSLETTER_PY_ARGS="
goto check_dependencies

:use_py
set "NEWSLETTER_PYTHON=py"
set "NEWSLETTER_PY_ARGS=-3"
goto check_dependencies

:use_python
set "NEWSLETTER_PYTHON=python"
set "NEWSLETTER_PY_ARGS="
goto check_dependencies

:use_bundled
set "NEWSLETTER_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "NEWSLETTER_PY_ARGS="

:check_dependencies
"%NEWSLETTER_PYTHON%" %NEWSLETTER_PY_ARGS% -c "import sys; sys.path.insert(0, '.vendor'); import pymupdf; import zxingcpp" >nul 2>&1
if not errorlevel 1 goto start_app
echo Setting up the PDF reader. An internet connection is needed for this first setup.
"%NEWSLETTER_PYTHON%" %NEWSLETTER_PY_ARGS% -m venv .venv
if errorlevel 1 goto setup_error
set "NEWSLETTER_PYTHON=.venv\Scripts\python.exe"
set "NEWSLETTER_PY_ARGS="
"%NEWSLETTER_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto setup_error

:start_app
"%NEWSLETTER_PYTHON%" %NEWSLETTER_PY_ARGS% server.py
if errorlevel 1 pause
exit /b

:setup_error
echo.
echo The PDF reader could not be installed. Check your internet connection and try again.
echo You can also open this folder in a terminal and run: python -m pip install -r requirements.txt
pause
exit /b 1
