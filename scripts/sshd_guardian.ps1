# FBSAT59 sshd guardian
# Re-ensures the OpenSSH Server is installed, registered, allowed through the
# firewall, has the Mac dev key authorised, and is running.
# Installed at C:\ProgramData\fbsat59-sshd-guardian.ps1
# Run by scheduled task "FBSAT59 sshd guardian" as SYSTEM (at startup + hourly).

$ErrorActionPreference = 'Continue'
$log = 'C:\ProgramData\fbsat59-sshd-guardian.log'
function Log($m) {
    try { "$(Get-Date -Format s) $m" | Out-File -Append -Encoding utf8 $log } catch {}
}

# Keep the log from growing without bound.
try {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 512KB)) {
        $tail = Get-Content $log -Tail 400
        Set-Content -Path $log -Value $tail -Encoding utf8
    }
} catch {}

$openssh = "$env:WINDIR\System32\OpenSSH"
$sshdExe = Join-Path $openssh 'sshd.exe'
$keygen  = Join-Path $openssh 'ssh-keygen.exe'

# 1. Binaries present?  If not, (re)add the OS capability.
if (-not (Test-Path $sshdExe)) {
    Log 'sshd.exe missing - Add-WindowsCapability OpenSSH.Server'
    try { Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null }
    catch { Log "Add-WindowsCapability failed: $_" }
}

# 2. Host keys + config dir.
if ((Test-Path $keygen) -and -not (Test-Path 'C:\ProgramData\ssh\ssh_host_ed25519_key')) {
    Log 'generating host keys (ssh-keygen -A)'
    try { & $keygen -A 2>&1 | Out-Null } catch { Log "ssh-keygen -A failed: $_" }
}
if (-not (Test-Path 'C:\ProgramData\ssh\sshd_config') -and (Test-Path (Join-Path $openssh 'sshd_config_default'))) {
    try {
        Copy-Item (Join-Path $openssh 'sshd_config_default') 'C:\ProgramData\ssh\sshd_config' -Force
        Log 'restored sshd_config from default'
    } catch { Log "restore sshd_config failed: $_" }
}

# 3. Authorised key for the Mac dev box.
$akf = 'C:\ProgramData\ssh\administrators_authorized_keys'
$keyBody = 'AAAAC3NzaC1lZDI1NTE5AAAAIIAoq1Gwt9j/JihA7WRevmePcGwpV0hd9q17ZN0rvrG+'
$keyLine = "ssh-ed25519 $keyBody sadatoshikoike@M2-Macbook-Air.local"
$havekey = $false
if (Test-Path $akf) {
    try { if ((Get-Content $akf -Raw) -like "*$keyBody*") { $havekey = $true } } catch {}
}
if (-not $havekey) {
    try {
        if (-not (Test-Path 'C:\ProgramData\ssh')) { New-Item -ItemType Directory -Path 'C:\ProgramData\ssh' -Force | Out-Null }
        Set-Content -Path $akf -Value $keyLine -Encoding ascii
        & icacls $akf /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
        Log 'restored administrators_authorized_keys'
    } catch { Log "restore authorized key failed: $_" }
}

# 4. Service registered?
$svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
if (-not $svc -and (Test-Path $sshdExe)) {
    Log 'sshd service missing - registering'
    try { New-Service -Name sshd -BinaryPathName "`"$sshdExe`"" -DisplayName 'OpenSSH SSH Server' -StartupType Automatic -ErrorAction Stop | Out-Null }
    catch { Log "New-Service failed: $_" }
    & sc.exe config sshd obj= LocalSystem | Out-Null
    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
}

# 4b. Failure-recovery actions (idempotent, cheap to re-apply).
if ($svc) {
    & sc.exe failure sshd reset= 86400 actions= restart/5000/restart/10000/restart/60000 | Out-Null
}

# 5. Firewall rule.
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {
    try {
        New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH SSH Server (sshd)' -Enabled True `
            -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
        Log 'restored firewall rule'
    } catch { Log "restore firewall rule failed: $_" }
}

# 6. Startup type + running state.
if ($svc) {
    try { if ($svc.StartType -ne 'Automatic') { Set-Service -Name sshd -StartupType Automatic; Log 'set StartType=Automatic' } } catch {}
    $svc.Refresh()
    if ($svc.Status -ne 'Running') {
        try { Start-Service sshd; Log "started sshd (was $($svc.Status))" } catch { Log "Start-Service failed: $_" }
    }
    $ag = Get-Service ssh-agent -ErrorAction SilentlyContinue
    if ($ag -and $ag.StartType -eq 'Disabled') {
        try { Set-Service ssh-agent -StartupType Manual; Log 'ssh-agent StartType Disabled->Manual' } catch {}
    }
}

$final = (Get-Service sshd -ErrorAction SilentlyContinue)
Log ("run complete; sshd=" + $(if ($final) { $final.Status } else { 'ABSENT' }))
