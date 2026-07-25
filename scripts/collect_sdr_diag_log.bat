@echo off
REM FBSAT59 SDR pipeline diagnostic log collector (Windows)
REM
REM Finds sdr_pipeline_diag.log (a separate, dedicated log added to help
REM diagnose audio "motorboating"/stutter reports) and copies it to the
REM Desktop so it's easy to attach to a GitHub issue comment.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM the file to your own Desktop for you to review and attach yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\sdr_pipeline_diag.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 SDR diagnostic log collector
echo ======================================
echo.
echo Looking for the log file at:
echo   %SRC%
echo.

if not exist "%SRC%" (
    echo NOT FOUND.
    echo.
    echo This file is only created once FBSAT59 has connected an SDR and
    echo streamed at least a second of data. Please make sure you've
    echo reproduced the audio issue at least once, then run this script
    echo again. If it still isn't found, please just tell us this exact
    echo message on the GitHub issue instead.
    echo.
    pause
    exit /b 1
)

set "OUT=%DEST%\fbsat59_sdr_diag_%STAMP%.txt"

copy /y "%SRC%" "%OUT%" >nul
if errorlevel 1 (
    echo Failed to copy the log file to the Desktop. Please copy it manually from:
    echo   %SRC%
    echo.
    pause
    exit /b 1
)

echo.
echo Done! Saved to your Desktop as:
echo   fbsat59_sdr_diag_%STAMP%.txt
echo.
echo Please attach this file to your reply on the GitHub issue
echo (you can drag and drop it directly into the comment box on github.com).
echo.
pause
