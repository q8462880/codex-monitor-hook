param(
    [string]$TargetDir = (Join-Path $HOME ".codex_screen"),
    [string]$CodexHome = "",
    [ValidateSet("full", "minimal")]
    [string]$HookProfile = "full",
    [switch]$SkipConfigUpdate,
    [switch]$SkipPipInstall
)

$ErrorActionPreference = "Stop"

if (-not $CodexHome) {
    if ($env:CODEX_HOME) {
        $CodexHome = $env:CODEX_HOME
    } else {
        $CodexHome = Join-Path $HOME ".codex"
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RelaySource = Join-Path $ScriptDir "codex_hook_relay.py"
$DaemonSource = Join-Path $ScriptDir "codex_screen_daemon.py"
$StateSource = Join-Path $ScriptDir "codex_state_manager.py"
$QuotaSource = Join-Path $ScriptDir "codex_quota_client.py"
$LogSource = Join-Path $ScriptDir "codex_screen_log.py"
$WindowsLauncherSource = Join-Path $ScriptDir "codex_hook_windows_launcher.ps1"
$ConfigUpdaterScript = Join-Path $ScriptDir "update_codex_config.py"
$RelayTarget = Join-Path $TargetDir "codex_hook_relay.py"
$DaemonTarget = Join-Path $TargetDir "codex_screen_daemon.py"
$StateTarget = Join-Path $TargetDir "codex_state_manager.py"
$QuotaTarget = Join-Path $TargetDir "codex_quota_client.py"
$LogTarget = Join-Path $TargetDir "codex_screen_log.py"
$WindowsLauncherTarget = Join-Path $TargetDir "codex_hook_windows_launcher.ps1"
if ($env:PYTHON) {
    $Python = $env:PYTHON
} else {
    $Python = "python"
}
$Pythonw = "pythonw"
$PythonCommand = Get-Command $Python -ErrorAction SilentlyContinue
if ($PythonCommand -and $PythonCommand.Path) {
    $PythonwCandidate = Join-Path (Split-Path -Parent $PythonCommand.Path) "pythonw.exe"
    if (Test-Path -LiteralPath $PythonwCandidate) {
        $Pythonw = $PythonwCandidate
    }
}
$ConfigPath = Join-Path $CodexHome "config.toml"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item -LiteralPath $RelaySource -Destination $RelayTarget -Force
Copy-Item -LiteralPath $DaemonSource -Destination $DaemonTarget -Force
Copy-Item -LiteralPath $StateSource -Destination $StateTarget -Force
Copy-Item -LiteralPath $QuotaSource -Destination $QuotaTarget -Force
Copy-Item -LiteralPath $LogSource -Destination $LogTarget -Force
Copy-Item -LiteralPath $WindowsLauncherSource -Destination $WindowsLauncherTarget -Force

if (-not $SkipPipInstall) {
    & $Python -m pip install hidapi
}

& $Python -m py_compile $RelayTarget $DaemonTarget $StateTarget $QuotaTarget $LogTarget $ConfigUpdaterScript
& $Python $DaemonTarget --self-test | Out-Host

if (-not $SkipConfigUpdate) {
    # The Python helper backs up config.toml before writing hooks and validates TOML.
    if (-not (Test-Path -LiteralPath $ConfigUpdaterScript)) {
        throw "Config updater script not found: $ConfigUpdaterScript"
    }
    & $Python $ConfigUpdaterScript $ConfigPath $RelayTarget $Pythonw $HookProfile
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to update Codex config.toml."
    }
}

Write-Host "Installed Codex screen hook files to $TargetDir"
if ($SkipConfigUpdate) {
    Write-Host "Skipped Codex config.toml update."
} else {
    Write-Host "Updated Codex config.toml at $ConfigPath"
    Write-Host "Installed hook profile: $HookProfile"
    Write-Host "Backup status was printed as BACKUP_PATH above; empty means a new config was created."
}
