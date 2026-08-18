@echo off
REM FBSAT59 diagnostic log collector - FT4 TX/RX timing (Windows)
REM
REM Finds fbsat59.log and the dedicated ft4_decode.log, copies them to the
REM Desktop, and also extracts just the FT4/PTT-related lines from
REM fbsat59.log into a smaller summary file so it's easy to read and share.
REM
REM Double-click this file to run it. No installation or admin rights
REM needed. Nothing is uploaded anywhere by this script - it only copies
REM files to your own Desktop for you to review and send yourself.

setlocal EnableDelayedExpansion

set "SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log"
set "FT4_SRC=%LOCALAPPDATA%\fbsat59\fbsat59\Logs\ft4_decode.log"
set "DEST=%USERPROFILE%\Desktop"
set "STAMP=%RANDOM%"

echo.
echo FBSAT59 log collector (FT4 TX/RX timing)
echo ===========================================
echo.
echo Looking for the log files at:
echo   %SRC%
echo   %FT4_SRC%
echo.

if not exist "%SRC%" (
    echo NOT FOUND: %SRC%
    echo.
    echo Please make sure FBSAT59 has been started at least once on this PC,
    echo then run this script again. If the file still isn't found, please
    echo just tell us this exact message instead.
    echo.
    pause
    exit /b 1
)

set "OUT_FULL=%DEST%\fbsat59_log_%STAMP%.txt"
set "OUT_SUMMARY=%DEST%\fbsat59_ft4_summary_%STAMP%.txt"
set "OUT_FT4=%DEST%\fbsat59_ft4_decode_%STAMP%.txt"

copy /y "%SRC%" "%OUT_FULL%" >nul
if errorlevel 1 (
    echo Failed to copy the log file to the Desktop. Please copy it manually from:
    echo   %SRC%
    echo.
    pause
    exit /b 1
)

>  "%OUT_SUMMARY%" echo FBSAT59 FT4/PTT related log lines (auto-extracted)
>> "%OUT_SUMMARY%" echo Generated: %DATE% %TIME%
>> "%OUT_SUMMARY%" echo.
findstr /I /C:"FT4" /C:"ft8lib" /C:"ft4wsjt" /C:"PTT" /C:"set_ptt" /C:"RigDirect" /C:"RigNet" /C:"Error" /C:"Exception" /C:"Traceback" "%SRC%" >> "%OUT_SUMMARY%"

if exist "%FT4_SRC%" (
    copy /y "%FT4_SRC%" "%OUT_FT4%" >nul
)

echo.
if exist "%OUT_FT4%" (
    echo Done! Three files were saved to your Desktop:
    echo   1) fbsat59_ft4_decode_%STAMP%.txt   ^(dedicated FT4 TX/RX timing log - please send this one first^)
    echo   2) fbsat59_ft4_summary_%STAMP%.txt  ^(just the FT4/PTT lines from the main log^)
    echo   3) fbsat59_log_%STAMP%.txt          ^(the full main log file^)
) else (
    echo NOTE: ft4_decode.log was not found at:
    echo   %FT4_SRC%
    echo ^(this file only appears after the FT4 tab has actually been opened
    echo  and has run through at least one RX/TX period^)
    echo.
    echo Two files were saved to your Desktop instead:
    echo   1) fbsat59_ft4_summary_%STAMP%.txt  ^(just the FT4/PTT lines from the main log^)
    echo   2) fbsat59_log_%STAMP%.txt          ^(the full main log file^)
)
echo.
echo Please send the file^(s^) back so we can look at what happened.
echo.
pause
