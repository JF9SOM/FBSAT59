@echo off
REM FBSAT59 diagnostic log collector - Autotrack / METEOR / SatDump (Windows)
REM
REM Finds fbsat59.log, copies the full file to the Desktop, and also
REM extracts just the Autotrack / METEOR / SatDump / WinError-related
REM lines into a second, much smaller file so it's easy to read and
REM share on GitHub.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM files to your own Desktop for you to review and attach yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 log collector (Autotrack / METEOR / SatDump)
echo ======================================================
echo.
echo Looking for the log file at:
echo   %SRC%
echo.

if not exist "%SRC%" (
    echo NOT FOUND.
    echo.
    echo Please make sure FBSAT59 has been started at least once on this PC,
    echo then run this script again. If the file still isn't found, please
    echo just tell us this exact message on the GitHub issue instead.
    echo.
    pause
    exit /b 1
)

set "OUT_FULL=%DEST%\fbsat59_log_%STAMP%.txt"
set "OUT_SUMMARY=%DEST%\fbsat59_autotrack_meteor_summary_%STAMP%.txt"

copy /y "%SRC%" "%OUT_FULL%" >nul
if errorlevel 1 (
    echo Failed to copy the log file to the Desktop. Please copy it manually from:
    echo   %SRC%
    echo.
    pause
    exit /b 1
)

>  "%OUT_SUMMARY%" echo FBSAT59 Autotrack / METEOR / SatDump / WinError related log lines (auto-extracted for GitHub issue)
>> "%OUT_SUMMARY%" echo Generated: %DATE% %TIME%
>> "%OUT_SUMMARY%" echo.
findstr /I /C:"Autotrack" /C:"METEOR" /C:"SatDump" /C:"satdump" /C:"WinError" /C:"SDR" /C:"RTL" /C:"librtlsdr" /C:"Rotator" /C:"Error" /C:"Exception" /C:"Traceback" "%SRC%" >> "%OUT_SUMMARY%"

echo.
echo Done! Two files were saved to your Desktop:
echo   1) fbsat59_log_%STAMP%.txt                       (the full log file)
echo   2) fbsat59_autotrack_meteor_summary_%STAMP%.txt   (just the Autotrack/METEOR/SatDump lines - please attach this one first)
echo.
echo One more thing, if you still have it open: the METEOR / HRPT tab has
echo its own separate log window (the small "Log" button near the top of
echo that tab) with SatDump's own output, including the exact line where
echo WinError 50 appeared. That window is not saved to disk automatically
echo - please click its "Save..." button once and attach that file too,
echo or copy/paste its text directly into the GitHub comment.
echo.
echo Please attach the file(s) to your reply on the GitHub issue
echo (you can drag and drop them directly into the comment box on github.com).
echo.
pause
