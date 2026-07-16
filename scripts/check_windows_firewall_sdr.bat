@echo off
REM FBSAT59 network / firewall diagnostic launcher (Windows)
REM
REM Double-click this file to run. It just launches the PowerShell script
REM in the same folder (check_windows_firewall_sdr.ps1) with a one-time
REM execution-policy bypass for this run only - nothing on your system is
REM changed permanently. Keep both files in the same folder.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_windows_firewall_sdr.ps1"
