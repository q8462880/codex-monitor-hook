param(
    [string]$TargetDir = (Join-Path $HOME ".codex_screen"),
    [string]$CodexHome = "",
    [ValidateSet("full", "minimal")]
    [string]$HookProfile = "full",
    [switch]$SkipConfigUpdate,
    [switch]$SkipPipInstall,
    [switch]$InstallPythonIfMissing
)

$ErrorActionPreference = "Stop"

function Test-PythonCommand {
    param([string]$Command)

    # 只运行最小 Python 代码，避免误把 Windows Store 的 python 占位命令当成可用解释器。
    try {
        & $Command -c "import sys; print(sys.executable)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Install-UserPython {
    $Winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python was not found and winget is unavailable. Install Python 3.11+ from https://www.python.org/downloads/ and re-run this installer."
    }

    # 普通用户机器可能没有 Python；带 -InstallPythonIfMissing 时尝试用 winget 做当前用户安装。
    # 不把这一步设为默认，是为了避免脚本在未明确授权时安装系统级软件。
    Write-Host "Python was not found. Installing Python 3.12 for current user with winget..."
    & winget install --id Python.Python.3.12 --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install Python. Install Python 3.11+ manually and re-run this installer."
    }
}

function Resolve-PythonCommand {
    # 优先使用用户明确指定的 PYTHON，其次尝试 Windows 常见的 py/python/python3。
    if ($env:PYTHON -and (Test-PythonCommand $env:PYTHON)) {
        return $env:PYTHON
    }

    foreach ($Candidate in @("py", "python", "python3")) {
        $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Found -and (Test-PythonCommand $Candidate)) {
            return $Candidate
        }
    }

    if ($InstallPythonIfMissing) {
        Install-UserPython
        foreach ($Candidate in @("py", "python", "python3")) {
            $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
            if ($Found -and (Test-PythonCommand $Candidate)) {
                return $Candidate
            }
        }
    }

    throw "Python 3.11+ was not found. Re-run with -InstallPythonIfMissing, or install Python from https://www.python.org/downloads/."
}

function Ensure-Pip {
    param([string]$Python)

    # hidapi 通过 pip 安装；如果 Python 没带 pip，先用标准库 ensurepip 自举。
    & $Python -m pip --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "pip was not found. Trying python -m ensurepip --upgrade..."
    & $Python -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "pip is unavailable and ensurepip failed. Install pip for this Python, then re-run this installer."
    }
}

function Ensure-Hidapi {
    param([string]$Python)

    # 依赖已存在时不重复安装，降低普通用户重复执行安装提示词时的失败概率。
    & $Python -c "import hid" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "hidapi is already installed for this Python."
        return
    }

    Ensure-Pip $Python
    Write-Host "Installing Python dependency: hidapi"
    & $Python -m pip install hidapi
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install hidapi. Check network access or install it manually with: $Python -m pip install hidapi"
    }
}

function Resolve-PythonwCommand {
    param([string]$Python)

    # Windows hook 使用 pythonw/pyw 可避免 daemon/relay 拉起时出现额外控制台窗口。
    $PythonCommand = Get-Command $Python -ErrorAction SilentlyContinue
    if ($PythonCommand -and $PythonCommand.Path) {
        $PythonwCandidate = Join-Path (Split-Path -Parent $PythonCommand.Path) "pythonw.exe"
        if (Test-Path -LiteralPath $PythonwCandidate) {
            return $PythonwCandidate
        }
    }
    foreach ($Candidate in @("pyw", "pythonw")) {
        $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Found) {
            return $Candidate
        }
    }
    return "pythonw"
}

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
$Python = Resolve-PythonCommand
$Pythonw = Resolve-PythonwCommand $Python
$ConfigPath = Join-Path $CodexHome "config.toml"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item -LiteralPath $RelaySource -Destination $RelayTarget -Force
Copy-Item -LiteralPath $DaemonSource -Destination $DaemonTarget -Force
Copy-Item -LiteralPath $StateSource -Destination $StateTarget -Force
Copy-Item -LiteralPath $QuotaSource -Destination $QuotaTarget -Force
Copy-Item -LiteralPath $LogSource -Destination $LogTarget -Force
Copy-Item -LiteralPath $WindowsLauncherSource -Destination $WindowsLauncherTarget -Force

if (-not $SkipPipInstall) {
    Ensure-Hidapi $Python
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

