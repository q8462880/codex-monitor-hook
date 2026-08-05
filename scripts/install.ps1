param(
    [string]$TargetDir = (Join-Path $HOME ".codex_screen"),
    [string]$CodexHome = "",
    [ValidateSet("full", "minimal")]
    [string]$HookProfile = "full",
    [switch]$SkipConfigUpdate
)

$ErrorActionPreference = "Stop"

$StartMarker = "# BEGIN codex-monitor-hook"
$EndMarker = "# END codex-monitor-hook"
$FullHookEvents = @(
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd"
)
$MinimalHookEvents = @(
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Stop",
    "SessionEnd"
)

function Assert-Windows {
    if ([Environment]::OSVersion.Platform -ne "Win32NT") {
        throw "This packaged installer currently supports Windows only."
    }
}

function ConvertTo-TomlString {
    param([string]$Value)

    if (-not $Value.Contains("'")) {
        return "'" + $Value + "'"
    }

    $Escaped = $Value.Replace("\", "\\").Replace('"', '\"')
    return '"' + $Escaped + '"'
}

function Get-HookEvents {
    if ($HookProfile -eq "minimal") {
        return $MinimalHookEvents
    }
    return $FullHookEvents
}

function New-HookBlock {
    param(
        [string]$RelayExe,
        [string]$Launcher
    )

    # Codex Windows hook 会通过 pwsh 执行 commandWindows。
    # 仍然保留 launcher，是为了第一时间隐藏 Codex 创建的外层控制台窗口。
    $CommandWindows = ConvertTo-TomlString "& `"$Launcher`" -RelayExe `"$RelayExe`""
    $Lines = New-Object System.Collections.Generic.List[string]
    $Lines.Add($StartMarker)

    foreach ($EventName in (Get-HookEvents)) {
        $Lines.Add("")
        $Lines.Add("[[hooks.$EventName]]")
        $Lines.Add("")
        $Lines.Add("[[hooks.$EventName.hooks]]")
        $Lines.Add('type = "command"')
        $Lines.Add('command = "~/.codex_screen/codex_hook_relay"')
        $Lines.Add("commandWindows = $CommandWindows")
        $Lines.Add("timeout = 5")
    }

    $Lines.Add($EndMarker)
    return ($Lines -join "`n")
}

function Backup-CodexConfig {
    param([string]$ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return ""
    }

    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupPath = "$ConfigPath.codex-monitor-hook.$Stamp.bak"
    Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath -Force
    return $BackupPath
}

function Get-PreservedSuffix {
    param([string]$OldBlock)

    # 老版本曾把 Codex 维护的 hooks.state 夹在标记块里。
    # 替换 hook 时保留这些表，避免用户重新确认 hook trust。
    $Positions = @()
    foreach ($Marker in @("`n[hooks.state]", "`n[plugins.", "`n[features]")) {
        $Position = $OldBlock.IndexOf($Marker)
        if ($Position -ge 0) {
            $Positions += $Position
        }
    }
    if ($Positions.Count -eq 0) {
        return ""
    }

    $Start = ($Positions | Measure-Object -Minimum).Minimum
    $Suffix = $OldBlock.Substring($Start).Trim()
    return ($Suffix -replace [regex]::Escape($EndMarker) + "\s*$", "").TrimEnd()
}

function Merge-HookBlock {
    param(
        [string]$OldText,
        [string]$Block
    )

    $Pattern = "(?s)" + [regex]::Escape($StartMarker) + ".*?" + [regex]::Escape($EndMarker)
    $Match = [regex]::Match($OldText, $Pattern)
    if ($Match.Success) {
        $Replacement = $Block
        $Suffix = Get-PreservedSuffix $Match.Value
        if ($Suffix) {
            $Replacement = $Replacement + "`n`n" + $Suffix
        }
        return $OldText.Substring(0, $Match.Index) + $Replacement + $OldText.Substring($Match.Index + $Match.Length)
    }

    $Prefix = $OldText.TrimEnd()
    if ($Prefix) {
        return $Prefix + "`n`n" + $Block
    }
    return $Block
}

function Update-CodexConfig {
    param(
        [string]$ConfigPath,
        [string]$RelayExe,
        [string]$Launcher
    )

    $ConfigDir = Split-Path -Parent $ConfigPath
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $BackupPath = Backup-CodexConfig $ConfigPath
    $OldText = ""
    if (Test-Path -LiteralPath $ConfigPath) {
        $OldText = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    }

    $Block = New-HookBlock $RelayExe $Launcher
    $NewText = Merge-HookBlock $OldText $Block
    Set-Content -LiteralPath $ConfigPath -Value $NewText.TrimEnd() -Encoding UTF8
    Write-Host "BACKUP_PATH=$BackupPath"
}

function Assert-FileExists {
    param(
        [string]$Path,
        [string]$Message
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw $Message
    }
}

Assert-Windows

if (-not $CodexHome) {
    if ($env:CODEX_HOME) {
        $CodexHome = $env:CODEX_HOME
    } else {
        $CodexHome = Join-Path $HOME ".codex"
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$BinDir = Join-Path $RepoRoot "bin\windows-x64"
$RelaySource = Join-Path $BinDir "codex_hook_relay.exe"
$DaemonSource = Join-Path $BinDir "codex_screen_daemon.exe"
$WindowsLauncherSource = Join-Path $ScriptDir "codex_hook_windows_launcher.ps1"
$RelayTarget = Join-Path $TargetDir "codex_hook_relay.exe"
$DaemonTarget = Join-Path $TargetDir "codex_screen_daemon.exe"
$WindowsLauncherTarget = Join-Path $TargetDir "codex_hook_windows_launcher.ps1"
$ConfigPath = Join-Path $CodexHome "config.toml"

Assert-FileExists $RelaySource "Missing packaged relay exe: $RelaySource"
Assert-FileExists $DaemonSource "Missing packaged daemon exe: $DaemonSource"
Assert-FileExists $WindowsLauncherSource "Missing Windows hook launcher: $WindowsLauncherSource"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item -LiteralPath $RelaySource -Destination $RelayTarget -Force
Copy-Item -LiteralPath $DaemonSource -Destination $DaemonTarget -Force
Copy-Item -LiteralPath $WindowsLauncherSource -Destination $WindowsLauncherTarget -Force

& $DaemonTarget --self-test | Out-Host
if (-not $?) {
    throw "Daemon self-test failed."
}

if (-not $SkipConfigUpdate) {
    Update-CodexConfig $ConfigPath $RelayTarget $WindowsLauncherTarget
}

Write-Host "Installed Codex screen hook files to $TargetDir"
if ($SkipConfigUpdate) {
    Write-Host "Skipped Codex config.toml update."
} else {
    Write-Host "Updated Codex config.toml at $ConfigPath"
    Write-Host "Installed hook profile: $HookProfile"
}


