param(
    [string]$TargetDir = (Join-Path $HOME ".codex_screen"),
    [string]$CodexHome = "",
    [ValidateSet("quota", "full", "minimal")]
    [string]$HookProfile = "quota",
    [switch]$SkipConfigUpdate
)

$ErrorActionPreference = "Stop"

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
$QuotaHookEvents = @(
    "SessionStart",
    "UserPromptSubmit"
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
        throw "This PowerShell installer currently supports Windows only."
    }
}

function Test-RealExecutablePath {
    param([string]$Path)

    if (-not $Path) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    $WindowsApps = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
    $FullPath = [IO.Path]::GetFullPath($Path)
    return -not $FullPath.StartsWith($WindowsApps, [System.StringComparison]::OrdinalIgnoreCase)
}

function Resolve-Python {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($Command -and (Test-RealExecutablePath $Command.Source)) {
        return $Command.Source
    }

    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher -and (Test-RealExecutablePath $Launcher.Source)) {
        Write-Warning "python.exe was not found or resolved to a WindowsApps alias; using py.exe as the installer runtime."
        return $Launcher.Source
    }

    throw "A real Python runtime was not found. Install Python from python.org or the Python launcher, then run this installer again."
}

function Resolve-Pythonw {
    param([string]$PythonPath)

    $Sibling = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
    if (Test-RealExecutablePath $Sibling) {
        return $Sibling
    }

    $Launcher = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($Launcher -and (Test-RealExecutablePath $Launcher.Source)) {
        return $Launcher.Source
    }

    $Command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($Command -and (Test-RealExecutablePath $Command.Source)) {
        return $Command.Source
    }

    throw "pythonw.exe or pyw.exe was not found beside Python. Install a standard Windows Python distribution so hooks do not open a console window."
}

function Ensure-HidApi {
    param([string]$PythonPath)

    & $PythonPath -c "import hid" 2>$null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "Installing the Python hidapi package for the current user..."
    # 限制网络重试，避免依赖源不可达时安装器长时间无输出。
    & $PythonPath -m pip install --user hidapi --disable-pip-version-check --timeout 15 --retries 1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install hidapi with the selected Python interpreter."
    }

    & $PythonPath -c "import hid" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "hidapi installation finished but Python still cannot import hid."
    }
}

function Get-HookEvents {
    param([string]$Profile)

    if ($Profile -eq "quota") {
        return $QuotaHookEvents
    }
    if ($Profile -eq "minimal") {
        return $MinimalHookEvents
    }
    return $FullHookEvents
}

function Test-PythonHookBootstrap {
    param(
        [string]$PythonPath
    )

    # Windows PowerShell 会重写 JSON 原生参数。安装阶段只验证用户 .pth
    # 已实际导入 bootstrap；JSON-first 顺序由真实 Codex Hook 运行时验证。
    & $PythonPath -c "import codex_hook_bootstrap; print('codex_hook_bootstrap OK')" | Out-Host
    return $LASTEXITCODE -eq 0
}

function Grant-CodexSandboxRuntimeAccess {
    param([string]$Path)

    # Hook 由 CodexSandboxUsers 的受限令牌执行。relay 需要写日志、单实例锁、
    # 会话缓存和动态端口文件；只给 RX 会让进程在入口阶段以 code 1 退出。
    $Account = "$env:COMPUTERNAME\CodexSandboxUsers"
    & icacls $Path /grant "${Account}:(OI)(CI)(M)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to grant Codex hook runtime access to $Account."
    }
}

function Stop-InstalledRuntimeProcess {
    param(
        [string[]]$ScriptTargets
    )

    # Python relay 和 daemon 没有固定的进程名，因此按完整命令行精确匹配。
    # 这样不会误杀用户运行的其他 Python 程序。
    $Processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    foreach ($Process in $Processes) {
        if (-not $Process.CommandLine) {
            continue
        }

        $Matched = $false
        foreach ($Target in $ScriptTargets) {
            if ($Process.CommandLine.IndexOf($Target, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $Matched = $true
                break
            }
        }
        if (-not $Matched) {
            continue
        }

        try {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
            Write-Host "Stopped existing Python runtime: PID=$($Process.ProcessId)"
        } catch {
            Write-Warning "Failed to stop Python runtime PID=$($Process.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Stop-InstalledExecutableProcess {
    param(
        [string[]]$ExecutableTargets
    )

    # 旧版本使用固定 exe 名称。按完整 exe 路径匹配，只处理本项目目录里的旧进程。
    foreach ($Process in (Get-Process -ErrorAction SilentlyContinue)) {
        $ProcessPath = $null
        try {
            $ProcessPath = $Process.Path
        } catch {
            continue
        }
        if (-not $ProcessPath) {
            continue
        }

        foreach ($Target in $ExecutableTargets) {
            $CurrentPath = [IO.Path]::GetFullPath($ProcessPath)
            $TargetPath = [IO.Path]::GetFullPath($Target)
            if ($CurrentPath -ieq $TargetPath) {
                Stop-Process -Id $Process.Id -Force -ErrorAction Stop
                Write-Host "Stopped legacy executable: PID=$($Process.Id) Path=$Target"
                break
            }
        }
    }
}

function Remove-LegacyRuntimeFiles {
    param(
        [string[]]$Paths
    )

    foreach ($Path in $Paths) {
        if (-not (Test-Path -LiteralPath $Path)) {
            continue
        }
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
        Write-Host "Removed legacy runtime file: $Path"
    }
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

$PythonPath = Resolve-Python
$PythonwPath = Resolve-Pythonw $PythonPath
Ensure-HidApi $PythonPath

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $CodexHome "config.toml"
$RelaySource = Join-Path $ScriptDir "codex_hook_relay.py"
$DaemonSource = Join-Path $ScriptDir "codex_screen_daemon.py"
$UpdateConfigSource = Join-Path $ScriptDir "update_codex_config.py"
$BootstrapInstallerSource = Join-Path $ScriptDir "install_python_hook_bootstrap.py"
$RelayTarget = Join-Path $TargetDir "codex_hook_relay.py"
$DaemonTarget = Join-Path $TargetDir "codex_screen_daemon.py"
$UpdateConfigTarget = Join-Path $TargetDir "update_codex_config.py"
$BootstrapInstallerTarget = Join-Path $TargetDir "install_python_hook_bootstrap.py"
$CommandLauncherTarget = Join-Path $TargetDir "codex_hook_relay.cmd"
$RelayExecutableTarget = Join-Path $TargetDir "codex_hook_relay.exe"
$DaemonExecutableTarget = Join-Path $TargetDir "codex_screen_daemon.exe"
$LegacyLauncher = Join-Path $TargetDir "codex_hook_windows_launcher.ps1"

Assert-FileExists $RelaySource "Missing Python relay script: $RelaySource"
Assert-FileExists $DaemonSource "Missing Python daemon script: $DaemonSource"
Assert-FileExists $UpdateConfigSource "Missing config updater script: $UpdateConfigSource"
Assert-FileExists $BootstrapInstallerSource "Missing Python Hook bootstrap installer: $BootstrapInstallerSource"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Grant-CodexSandboxRuntimeAccess $TargetDir
Stop-InstalledExecutableProcess @($RelayExecutableTarget, $DaemonExecutableTarget)
Remove-LegacyRuntimeFiles @($LegacyLauncher, $CommandLauncherTarget, $RelayExecutableTarget, $DaemonExecutableTarget)

# relay 和 daemon 会导入同目录下的多个 Python 模块，统一复制整个 scripts 目录
# 中的 .py 文件，避免只复制入口文件后在用户电脑上出现隐蔽的导入错误。
Get-ChildItem -LiteralPath $ScriptDir -Filter "*.py" -File |
    Copy-Item -Destination $TargetDir -Force

# Python 在尝试打开命令行脚本前会加载用户 site-packages。这个条件化
# bootstrap 仅处理 Codex 注入的 Hook JSON，普通 Python 进程不会受影响。
& $PythonPath $BootstrapInstallerTarget $TargetDir
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the Python Hook bootstrap."
}

# 使用与 Windows runner 相同的“JSON 在脚本前”顺序验证用户 site 的 .pth
# 已被当前 Python 实际加载。dry-run 不会打开 HID、写日志或拉起 daemon。
$PreviousBootstrapDryRun = $env:CODEX_HOOK_BOOTSTRAP_DRY_RUN
try {
    $env:CODEX_HOOK_BOOTSTRAP_DRY_RUN = "1"
    if (-not (Test-PythonHookBootstrap $PythonPath)) {
        throw "Python Hook bootstrap validation failed."
    }
} finally {
    $env:CODEX_HOOK_BOOTSTRAP_DRY_RUN = $PreviousBootstrapDryRun
}

& $PythonPath $DaemonTarget --self-test | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Python daemon self-test failed."
}

if (-not $SkipConfigUpdate) {
    & $PythonPath $UpdateConfigTarget `
        $ConfigPath `
        $PythonwPath `
        $RelayTarget `
        $HookProfile | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to update Codex config.toml."
    }

    # 通过 Codex 自己的 hooks/list 接口检查 Hook 是否已加载和信任。
    $OldCodexHome = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $CodexHome
        & $PythonPath $DaemonTarget `
            --diagnose-hooks (Split-Path -Parent $ScriptDir) `
            --expected-hook-count (Get-HookEvents $HookProfile).Count `
            --hook-diagnostic-timeout 3 | Out-Host
        $HookDiagnosticExit = $LASTEXITCODE
    } finally {
        $env:CODEX_HOME = $OldCodexHome
    }
    if ($HookDiagnosticExit -ne 0) {
        Write-Warning "Codex hooks are installed but not executable yet. Restart Codex, open Settings > Hooks, then review and trust the installed hooks."
    }
}

# 只有所有校验和配置写入成功后才停止旧 daemon，失败时保留可用旧实例。
Stop-InstalledRuntimeProcess @($RelayTarget, $DaemonTarget)

Write-Host "Installed Codex screen hook runtime files to $TargetDir"
Write-Host "Python interpreter: $PythonPath"
Write-Host "Hook Python interpreter: $PythonwPath"
if ($SkipConfigUpdate) {
    Write-Host "Skipped Codex config.toml update."
} else {
    Write-Host "Updated Codex config.toml at $ConfigPath"
    Write-Host "Installed hook profile: $HookProfile"
}
