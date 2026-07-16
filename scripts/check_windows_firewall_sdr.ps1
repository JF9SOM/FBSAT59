# FBSAT59 network / firewall diagnostic (Windows)
#
# Read-only: this script does not change any settings. It collects:
#   - your current network profile (Public/Private/Domain)
#   - the Windows Firewall on/off state and default action per profile
#   - any firewall rule that specifically mentions FBSAT59
#   - (optional) a connectivity test to a remote SDR host:port you enter
#   - recent Windows Defender detections, if any
#
# and saves everything to a text file on your Desktop so it's easy to
# copy/paste or attach to a GitHub issue.
#
# Run this by double-clicking check_windows_firewall_sdr.bat in the same
# folder - it launches this script for you.

$ErrorActionPreference = 'SilentlyContinue'

$stamp = Get-Random
$outFile = Join-Path ([Environment]::GetFolderPath('Desktop')) "fbsat59_firewall_check_$stamp.txt"

$lines = New-Object System.Collections.Generic.List[string]
function Add-Line($text = '') { $lines.Add($text) }

Add-Line '===== FBSAT59 network / firewall diagnostic ====='
Add-Line "Generated: $(Get-Date)"
Add-Line ''

# ---------------------------------------------------------------------
# Locate FBSAT59.exe (installer writes HKLM\Software\FBSAT59\InstallDir)
# ---------------------------------------------------------------------
$installDir = $null
try {
    $installDir = (Get-ItemProperty -Path 'HKLM:\Software\FBSAT59' -Name 'InstallDir' -ErrorAction Stop).InstallDir
} catch {}
if (-not $installDir) {
    $candidate = Join-Path $env:ProgramFiles 'FBSAT59'
    if (Test-Path $candidate) { $installDir = $candidate }
}
$exePath = $null
if ($installDir) {
    $candidateExe = Join-Path $installDir 'FBSAT59.exe'
    if (Test-Path $candidateExe) { $exePath = $candidateExe }
}
Add-Line '----- FBSAT59 install location -----'
if ($exePath) {
    Add-Line "Found: $exePath"
} else {
    Add-Line "Could not auto-detect FBSAT59.exe (checked registry and $env:ProgramFiles\FBSAT59). Firewall-rule matching below falls back to name-only search."
}
Add-Line ''

# ---------------------------------------------------------------------
# Network profile (Public networks are firewalled more strictly)
# ---------------------------------------------------------------------
Add-Line '----- Network profile -----'
try {
    $profiles = Get-NetConnectionProfile
    if ($profiles) {
        foreach ($p in $profiles) {
            Add-Line "Name=$($p.Name)  InterfaceAlias=$($p.InterfaceAlias)  NetworkCategory=$($p.NetworkCategory)"
        }
    } else {
        Add-Line 'Get-NetConnectionProfile returned nothing.'
    }
} catch {
    Add-Line "Could not read network profile: $($_.Exception.Message)"
}
Add-Line ''

# ---------------------------------------------------------------------
# Firewall on/off + default action, per profile
# ---------------------------------------------------------------------
Add-Line '----- Windows Firewall status per profile -----'
try {
    $fwProfiles = Get-NetFirewallProfile
    foreach ($fp in $fwProfiles) {
        Add-Line "$($fp.Name): Enabled=$($fp.Enabled)  DefaultInboundAction=$($fp.DefaultInboundAction)  DefaultOutboundAction=$($fp.DefaultOutboundAction)"
    }
} catch {
    Add-Line "Could not read firewall profile status: $($_.Exception.Message)"
}
Add-Line ''

# ---------------------------------------------------------------------
# Any firewall rule that specifically mentions FBSAT59
# ---------------------------------------------------------------------
Add-Line '----- Firewall rules mentioning FBSAT59 -----'
try {
    $appFilters = Get-NetFirewallApplicationFilter | Where-Object {
        $_.Program -and $_.Program -like '*FBSAT59*'
    }
    if ($appFilters) {
        foreach ($af in $appFilters) {
            $rule = $af | Get-NetFirewallRule
            Add-Line "$($rule.DisplayName): Direction=$($rule.Direction) Action=$($rule.Action) Enabled=$($rule.Enabled) Program=$($af.Program)"
        }
    } else {
        Add-Line 'No firewall rule mentions FBSAT59 by name.'
        Add-Line 'This is normal if Windows never showed an Allow/Block prompt for it -'
        Add-Line 'in that case the "DefaultInboundAction/DefaultOutboundAction" above is what applies.'
    }
} catch {
    Add-Line "Could not query firewall rules: $($_.Exception.Message)"
}
Add-Line ''

# ---------------------------------------------------------------------
# Optional connectivity re-test to the remote SDR
# ---------------------------------------------------------------------
$remoteHost = Read-Host 'Enter the remote SDR IP address (e.g. 192.168.1.81), or press Enter to skip'
if ($remoteHost) {
    $remotePortInput = Read-Host 'Enter the remote SDR port (press Enter for default 55132)'
    $remotePort = 55132
    if ($remotePortInput) { $remotePort = [int]$remotePortInput }

    Add-Line "----- Connectivity test to ${remoteHost}:${remotePort} -----"
    try {
        $test = Test-NetConnection -ComputerName $remoteHost -Port $remotePort
        Add-Line "TcpTestSucceeded=$($test.TcpTestSucceeded)  RemoteAddress=$($test.RemoteAddress)  SourceAddress=$($test.SourceAddress)"
    } catch {
        Add-Line "Test-NetConnection failed: $($_.Exception.Message)"
    }
    Add-Line ''
}

# ---------------------------------------------------------------------
# Recent Windows Defender detections (skipped gracefully if using a
# different antivirus, or if Defender's PowerShell module is unavailable)
# ---------------------------------------------------------------------
Add-Line '----- Recent Windows Defender detections (last 7 days) -----'
try {
    $threats = Get-MpThreatDetection | Where-Object {
        $_.InitialDetectionTime -gt (Get-Date).AddDays(-7)
    }
    if ($threats) {
        foreach ($t in $threats) {
            Add-Line "$($t.InitialDetectionTime): $($t.ThreatName) -- $($t.Resources)"
        }
    } else {
        Add-Line 'No recent Defender detections found (or Defender is not the active antivirus).'
    }
} catch {
    Add-Line 'Could not query Windows Defender (may be using a different antivirus).'
}
Add-Line ''

Add-Line '===== End of diagnostic ====='

$lines | Out-File -FilePath $outFile -Encoding utf8

Write-Host ''
Write-Host ($lines -join "`r`n")
Write-Host ''
Write-Host "Done! Results saved to your Desktop:"
Write-Host "  $outFile"
Write-Host ''
Write-Host 'Please copy/paste the contents of that file into your GitHub reply'
Write-Host '(or drag the file itself into the comment box).'
Write-Host ''
Read-Host 'Press Enter to close this window'
