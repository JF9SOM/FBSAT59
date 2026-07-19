@echo off
REM FBSAT59 Remote SDR thread diagnostic launcher (Windows, GitHub Issue #12)
REM
REM Runs check_soapy_remote_thread_test.py in the same folder, using
REM Python 3.11 (matching the Python version FBSAT59 itself was built
REM with) and FBSAT59's own bundled SoapySDR files. Nothing else needs
REM to be installed except Python 3.11 itself if you don't already have it.
REM
REM Keep both files in the same folder, then double-click this one.

echo.
echo FBSAT59 Remote SDR thread diagnostic
echo ======================================
echo.
echo This needs Python 3.11 (matching the Python version FBSAT59 was built
echo with). It reuses FBSAT59's own bundled SoapySDR files - nothing else
echo is required.
echo.
echo If Python 3.11 is not installed, get it from:
echo   https://www.python.org/downloads/
echo (pick a 3.11.x release, 64-bit Windows installer, and check
echo  "Add python.exe to PATH" during setup)
echo.

set /p SDR_HOST="Enter the remote SDR IP address (e.g. 192.168.1.81): "
if "%SDR_HOST%"=="" (
    echo No IP address entered - exiting.
    pause
    exit /b 1
)
set /p SDR_PORT="Enter the remote SDR port (press Enter for default 55132): "
if "%SDR_PORT%"=="" set "SDR_PORT=55132"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 "%~dp0check_soapy_remote_thread_test.py" %SDR_HOST% %SDR_PORT%
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0check_soapy_remote_thread_test.py" %SDR_HOST% %SDR_PORT%
    goto :done
)

echo.
echo Python was not found on this PC. Please install Python 3.11 first
echo (see the link above), then run this script again.

:done
echo.
pause
