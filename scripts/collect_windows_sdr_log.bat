@echo off
REM FBSAT59 diagnostic log collector (Windows)
REM
REM Finds fbsat59.log, copies the full file to the Desktop, and also
REM extracts just the SDR / Remote-SDR / connection-related lines into a
REM second, much smaller file so it's easy to read and share on GitHub.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM files to your own Desktop for you to review and attach yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 log collector
echo ======================
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
set "OUT_SUMMARY=%DEST%\fbsat59_sdr_summary_%STAMP%.txt"

copy /y "%SRC%" "%OUT_FULL%" >nul
if errorlevel 1 (
    echo Failed to copy the log file to the Desktop. Please copy it manually from:
    echo   %SRC%
    echo.
    pause
    exit /b 1
)

>  "%OUT_SUMMARY%" echo FBSAT59 SDR / Remote-related log lines (auto-extracted for GitHub issue)
>> "%OUT_SUMMARY%" echo Generated: %DATE% %TIME%
>> "%OUT_SUMMARY%" echo.
findstr /I /C:"SDR" /C:"Soapy" /C:"remote" /C:"Rig1" /C:"Rig 1" /C:"RigConnect" /C:"Error" /C:"Exception" /C:"Traceback" "%SRC%" >> "%OUT_SUMMARY%"

echo.
echo Done! Two files were saved to your Desktop:
echo   1) fbsat59_log_%STAMP%.txt          (the full log file)
echo   2) fbsat59_sdr_summary_%STAMP%.txt  (just the SDR / Remote lines - please attach this one first)
echo.
echo Please attach BOTH files to your reply on the GitHub issue
echo (you can drag and drop them directly into the comment box on github.com).
echo.
pause
