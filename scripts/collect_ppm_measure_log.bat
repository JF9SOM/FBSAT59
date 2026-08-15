@echo off
REM FBSAT59 diagnostic log collector - SDR PPM measurement (Windows)
REM
REM Finds fbsat59.log and extracts just the "PPM measure" lines from the
REM automatic SDR clock-drift measurement feature (Rig Settings > SDR
REM Settings > Measure...) into a small file on the Desktop, easy to
REM read and share.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM a file to your own Desktop for you to review and send yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 log collector (SDR PPM measurement)
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

set "OUT_SUMMARY=%DEST%\fbsat59_ppm_measure_%STAMP%.txt"

>  "%OUT_SUMMARY%" echo FBSAT59 SDR PPM measurement log lines (auto-extracted)
>> "%OUT_SUMMARY%" echo Generated: %DATE% %TIME%
>> "%OUT_SUMMARY%" echo.
findstr /I /C:"PPM measure" "%SRC%" >> "%OUT_SUMMARY%"

echo.
echo Done! One file was saved to your Desktop:
echo   fbsat59_ppm_measure_%STAMP%.txt
echo.
echo Please send this file back so we can look at what happened.
echo.
pause
