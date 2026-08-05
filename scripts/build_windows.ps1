param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$BinDir = Join-Path $RepoRoot "bin\windows-x64"
$BuildDir = Join-Path $RepoRoot "build\pyinstaller"
$SpecDir = Join-Path $BuildDir "spec"

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Force -Path $SpecDir | Out-Null

# 这个脚本只给维护者发布前使用；普通用户安装包里已经带 exe，不需要 Python。
& $Python -m PyInstaller --version
if (-not $?) {
    throw "PyInstaller is not installed. Run: $Python -m pip install pyinstaller"
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name codex_hook_relay `
    --paths $ScriptDir `
    --hidden-import codex_screen_log `
    --distpath $BinDir `
    --workpath (Join-Path $BuildDir "relay") `
    --specpath $SpecDir `
    (Join-Path $ScriptDir "codex_hook_relay.py")
if (-not $?) {
    throw "Failed to build codex_hook_relay.exe"
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name codex_screen_daemon `
    --paths $ScriptDir `
    --hidden-import hid `
    --hidden-import codex_screen_log `
    --hidden-import codex_state_manager `
    --hidden-import codex_quota_client `
    --distpath $BinDir `
    --workpath (Join-Path $BuildDir "daemon") `
    --specpath $SpecDir `
    (Join-Path $ScriptDir "codex_screen_daemon.py")
if (-not $?) {
    throw "Failed to build codex_screen_daemon.exe"
}

& (Join-Path $BinDir "codex_screen_daemon.exe") --self-test | Out-Host
if (-not $?) {
    throw "Packaged daemon self-test failed."
}

Write-Host "Built Windows package files in $BinDir"


