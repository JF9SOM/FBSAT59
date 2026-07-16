# FBSAT59 SoapySDR module load diagnostic (Windows)
#
# Read-only: this script does not change any settings or files. It:
#   - finds every DLL in FBSAT59's bundled soapy_modules folder (the SDR
#     device-driver modules: remote, rtlsdr, hackrf, airspy, airspyhf)
#     plus the SoapySDR core DLL in the install root
#   - for each one, records its size / last-write time / whether Windows
#     has marked it as "downloaded from the internet" (Zone.Identifier)
#   - attempts to load each one directly via the Win32 LoadLibrary API
#     (bypassing SoapySDR's own module scanner entirely) and records
#     success/failure plus the exact Win32 error code on failure
#   - checks whether Windows 11 "Smart App Control" is enforcing a
#     stricter code-trust policy that can silently block unsigned DLLs
#
# The goal is to tell apart two possibilities:
#   (a) something is wrong with the "remote" module specifically
#   (b) Windows can't load ANY of FBSAT59's bundled SoapySDR DLLs, which
#       would point to a more general problem unrelated to Remote SDR
#
# Run this by double-clicking check_windows_soapy_module_load.bat in the
# same folder - it launches this script for you.

$ErrorActionPreference = 'SilentlyContinue'

$stamp = Get-Random
$outFile = Join-Path ([Environment]::GetFolderPath('Desktop')) "fbsat59_soapy_module_check_$stamp.txt"

$lines = New-Object System.Collections.Generic.List[string]
function Add-Line($text = '') { $lines.Add($text) }

Add-Line '===== FBSAT59 SoapySDR module load diagnostic ====='
Add-Line "Generated: $(Get-Date)"
Add-Line ''

# ---------------------------------------------------------------------
# Locate the FBSAT59 install directory (installer writes this registry
# value; fall back to the default Program Files path if not found).
# ---------------------------------------------------------------------
$installDir = $null
try {
    $installDir = (Get-ItemProperty -Path 'HKLM:\Software\FBSAT59' -Name 'InstallDir' -ErrorAction Stop).InstallDir
} catch {}
if (-not $installDir) {
    $candidate = Join-Path $env:ProgramFiles 'FBSAT59'
    if (Test-Path $candidate) { $installDir = $candidate }
}

Add-Line '----- FBSAT59 install location -----'
if ($installDir) {
    Add-Line "Found: $installDir"
} else {
    Add-Line 'Could not auto-detect the FBSAT59 install directory (checked registry and Program Files). Stopping - nothing more this script can check.'
    Add-Line ''
    Add-Line '===== End of diagnostic ====='
    $lines | Out-File -FilePath $outFile -Encoding utf8
    Write-Host ($lines -join "`r`n")
    Read-Host 'Press Enter to close this window'
    exit 1
}
Add-Line ''

$internalDir = Join-Path $installDir '_internal'
$modulesDir = Join-Path $internalDir 'soapy_modules'

# ---------------------------------------------------------------------
# Win32 LoadLibrary / FreeLibrary via P/Invoke - lets us test loading a
# DLL directly, completely independent of SoapySDR's own module scanner.
#
# SetDllDirectory is required here: FBSAT59.exe's own main.py calls
# os.add_dll_directory() on the "_internal" folder at startup precisely
# so that soapy_modules\*.dll can resolve their dependency on
# _internal\SoapySDR.dll (and sibling flat-bundled DLLs like
# libhackrf/librtlsdr/libairspy*). A bare PowerShell process has no such
# search-path addition, so without calling SetDllDirectory ourselves,
# every module DLL would fail to resolve that same dependency and report
# ERROR_MOD_NOT_FOUND (126) regardless of whether anything is actually
# wrong - that would test our own script's search path, not FBSAT59's.
# ---------------------------------------------------------------------
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NativeLib {
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern IntPtr LoadLibrary(string lpFileName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool FreeLibrary(IntPtr hModule);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern bool SetDllDirectory(string lpPathName);
}
"@

Add-Line '----- DLL search path setup -----'
if ([NativeLib]::SetDllDirectory($internalDir)) {
    Add-Line "SetDllDirectory succeeded: $internalDir added to this process's DLL search path, matching what FBSAT59.exe itself does at startup."
} else {
    Add-Line "WARNING: SetDllDirectory failed for $internalDir - results below may show spurious failures unrelated to FBSAT59's real behavior."
}
Add-Line ''

function Test-DllLoad {
    param([string]$Path)

    $result = [ordered]@{
        Path = $Path
        Exists = $false
        SizeBytes = $null
        LastWriteTime = $null
        HasZoneIdentifier = $false
        LoadLibrarySucceeded = $false
        Win32Error = $null
    }

    if (-not (Test-Path $Path)) {
        return $result
    }
    $result.Exists = $true

    $item = Get-Item $Path
    $result.SizeBytes = $item.Length
    $result.LastWriteTime = $item.LastWriteTime

    $zone = Get-Item -Path $Path -Stream 'Zone.Identifier' -ErrorAction SilentlyContinue
    $result.HasZoneIdentifier = [bool]$zone

    $handle = [NativeLib]::LoadLibrary($Path)
    if ($handle -ne [IntPtr]::Zero) {
        $result.LoadLibrarySucceeded = $true
        [void][NativeLib]::FreeLibrary($handle)
    } else {
        $result.Win32Error = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    }

    return $result
}

function Format-DllResult($r) {
    if (-not $r.Exists) {
        return "$($r.Path): FILE NOT FOUND"
    }
    $zoneText = if ($r.HasZoneIdentifier) { 'YES (marked as downloaded from the internet)' } else { 'no' }
    if ($r.LoadLibrarySucceeded) {
        return "$($r.Path): size=$($r.SizeBytes) bytes  modified=$($r.LastWriteTime)  internet-mark=$zoneText  LoadLibrary=SUCCESS"
    } else {
        return "$($r.Path): size=$($r.SizeBytes) bytes  modified=$($r.LastWriteTime)  internet-mark=$zoneText  LoadLibrary=FAILED (Win32 error code $($r.Win32Error))"
    }
}

# ---------------------------------------------------------------------
# Test every DLL in soapy_modules (remote, rtlsdr, hackrf, airspy, airspyhf)
# ---------------------------------------------------------------------
Add-Line '----- soapy_modules DLLs (device driver modules) -----'
if (Test-Path $modulesDir) {
    $moduleDlls = Get-ChildItem -Path $modulesDir -Filter '*.dll' -ErrorAction SilentlyContinue
    if ($moduleDlls) {
        foreach ($dll in $moduleDlls) {
            $r = Test-DllLoad -Path $dll.FullName
            Add-Line (Format-DllResult $r)
        }
    } else {
        Add-Line "No .dll files found in $modulesDir"
    }
} else {
    Add-Line "Folder not found: $modulesDir"
}
Add-Line ''

# ---------------------------------------------------------------------
# Test the SoapySDR core DLL(s) in the install root, for comparison.
# ---------------------------------------------------------------------
Add-Line '----- SoapySDR core DLL(s) in install root -----'
if (Test-Path $internalDir) {
    $coreDlls = Get-ChildItem -Path $internalDir -Filter 'SoapySDR*.dll' -ErrorAction SilentlyContinue
    if ($coreDlls) {
        foreach ($dll in $coreDlls) {
            $r = Test-DllLoad -Path $dll.FullName
            Add-Line (Format-DllResult $r)
        }
    } else {
        Add-Line "No SoapySDR*.dll files found in $internalDir"
    }
} else {
    Add-Line "Folder not found: $internalDir"
}
Add-Line ''

# ---------------------------------------------------------------------
# Windows 11 "Smart App Control" - a stricter code-trust policy that can
# silently block unsigned/unrecognized binaries (including DLLs loaded
# into a trusted process) with no visible warning to the user.
# ---------------------------------------------------------------------
Add-Line '----- Smart App Control status -----'
try {
    $sacValue = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name 'VerifiedAndReputablePolicyState' -ErrorAction Stop).VerifiedAndReputablePolicyState
    switch ($sacValue) {
        0 { Add-Line 'Smart App Control: Off' }
        1 { Add-Line 'Smart App Control: Enforced (this can silently block unsigned DLLs - may be relevant here)' }
        2 { Add-Line 'Smart App Control: Evaluation/Audit mode' }
        default { Add-Line "Smart App Control: unknown state (registry value = $sacValue)" }
    }
} catch {
    Add-Line 'Smart App Control: registry value not found (likely means this Windows version/edition does not have the feature, or it was never enabled).'
}
Add-Line ''

Add-Line '===== End of diagnostic ====='

$lines | Out-File -FilePath $outFile -Encoding utf8

Write-Host ''
Write-Host ($lines -join "`r`n")
Write-Host ''
Write-Host 'Done! Results saved to your Desktop:'
Write-Host "  $outFile"
Write-Host ''
Write-Host 'Please copy/paste the contents of that file into your GitHub reply'
Write-Host '(or drag the file itself into the comment box).'
Write-Host ''
Read-Host 'Press Enter to close this window'
