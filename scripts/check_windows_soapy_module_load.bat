@echo off
REM FBSAT59 SoapySDR module load diagnostic launcher (Windows)
REM
REM Double-click this file to run. It just launches the PowerShell script
REM in the same folder (check_windows_soapy_module_load.ps1) with a
REM one-time execution-policy bypass for this run only - nothing on your
REM system is changed permanently. Keep both files in the same folder.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_windows_soapy_module_load.ps1"
