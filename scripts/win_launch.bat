@echo off
REM =====================================================================
REM FBSAT59 - Windows one-click launcher (run from a source checkout)
REM
REM Double-click the desktop shortcut that points here.  On each start it:
REM   1. git pull --ff-only            (offline / local edits -> skipped)
REM   2. pip install -e .[dev]         (only when pyproject.toml changed)
REM   3. bootstrap_natives.py          (download Hamlib/ft8lib/q65lib/
REM                                     ft4wsjt/direwolf/CW model if missing)
REM   4. python -m src.main
REM
REM The console stays open while the app runs (so you can watch the log)
REM and closes automatically when the app exits.  There is no "pause".
REM =====================================================================

setlocal EnableExtensions
title FBSAT59
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" goto no_venv

echo [FBSAT59] Updating source...
set "HEAD_BEFORE="
for /f "delims=" %%H in ('git rev-parse HEAD 2^>nul') do set "HEAD_BEFORE=%%H"
git pull --ff-only
if errorlevel 1 echo [FBSAT59] git pull skipped (offline or local changes) - using the current checkout.

set "HEAD_AFTER="
for /f "delims=" %%H in ('git rev-parse HEAD 2^>nul') do set "HEAD_AFTER=%%H"
if not "%HEAD_BEFORE%"=="%HEAD_AFTER%" call :maybe_reinstall

echo [FBSAT59] Checking native components (Hamlib / ft8lib / q65lib / ft4wsjt / direwolf / CW model / lameenc)...
"%PY%" scripts\bootstrap_natives.py

echo [FBSAT59] Starting FBSAT59...
"%PY%" -m src.main
set "RC=%ERRORLEVEL%"
echo [FBSAT59] FBSAT59 exited (code %RC%).
endlocal & exit /b %RC%

:maybe_reinstall
git diff --name-only "%HEAD_BEFORE%" "%HEAD_AFTER%" 2>nul | findstr /I /C:"pyproject.toml" >nul
if errorlevel 1 goto :eof
echo [FBSAT59] Dependencies changed - running pip install -e .[dev]...
"%PY%" -m pip install -e .[dev] -q
goto :eof

:no_venv
echo.
echo [FBSAT59] Python venv not found at:
echo     %CD%\%PY%
echo.
echo [FBSAT59] Create it once, then start this launcher again:
echo     py -3.11 -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -e .[dev]
echo.
pause
endlocal & exit /b 1
