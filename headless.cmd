@echo off
rem comfyui-headless launcher for Windows. HEADLESS_PYTHON selects the interpreter.
setlocal
set "HEADLESS_ROOT=%~dp0"
set "HEADLESS_PY_ARGS="
if defined HEADLESS_PYTHON goto run
where py >nul 2>nul
if errorlevel 1 goto try_python
set "HEADLESS_PYTHON=py"
set "HEADLESS_PY_ARGS=-3"
goto run
:try_python
where python >nul 2>nul
if errorlevel 1 goto missing
set "HEADLESS_PYTHON=python"
goto run
:missing
echo comfyui-headless: Python 3.11 or newer was not found. Install it or set HEADLESS_PYTHON. 1>&2
exit /b 1
:run
"%HEADLESS_PYTHON%" %HEADLESS_PY_ARGS% "%HEADLESS_ROOT%headless.py" %*
exit /b %ERRORLEVEL%
