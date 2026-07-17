@echo off
REM FBSAT59 diagnostic log collector - Rig / Hamlib / CI-V (Windows)
REM
REM Finds fbsat59.log, copies the full file to the Desktop, and also
REM extracts just the Rig / Hamlib / CI-V / connection-related lines into
REM a second, much smaller file so it's easy to read and share.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM files to your own Desktop for you to review and send yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 log collector (Rig / Hamlib / CI-V)
echo =============================================
echo.
echo Looking for the log file at:
echo   %SRC%
echo.

if not exist "%SRC%" (
    echo NOT FOUND.
    echo.
    echo Please make sure FBSAT59 has been started at least once on this PC,
    echo then run this script again. If the file still isn't found, please
    echo just tell us this exact message instead.
    echo.
    pause
    exit /b 1
)

set "OUT_FULL=%DEST%\fbsat59_log_%STAMP%.txt"
set "OUT_SUMMARY=%DEST%\fbsat59_rig_summary_%STAMP%.txt"

copy /y "%SRC%" "%OUT_FULL%" >nul
if errorlevel 1 (
    echo Failed to copy the log file to the Desktop. Please copy it manually from:
    echo   %SRC%
    echo.
    pause
    exit /b 1
)

>  "%OUT_SUMMARY%" echo FBSAT59 Rig / Hamlib / CI-V related log lines (auto-extracted)
>> "%OUT_SUMMARY%" echo Generated: %DATE% %TIME%
>> "%OUT_SUMMARY%" echo.
findstr /I /C:"RigDirect" /C:"RigNet" /C:"RigConnect" /C:"Hamlib" /C:"CI-V" /C:"civaddr" /C:"satmode" /C:"SATMODE" /C:"Mode/CTCSS" /C:"RIG:" /C:"Rig1Settings" /C:"Rig2Settings" /C:"Error" /C:"Exception" /C:"Traceback" "%SRC%" >> "%OUT_SUMMARY%"

echo.
echo Done! Two files were saved to your Desktop:
echo   1) fbsat59_log_%STAMP%.txt          (the full log file)
echo   2) fbsat59_rig_summary_%STAMP%.txt  (just the Rig / Hamlib lines - please send this one first)
echo.
echo Please send BOTH files back so we can look at what happened.
echo.
pause
