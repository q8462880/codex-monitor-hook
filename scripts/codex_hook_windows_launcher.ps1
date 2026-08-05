param(
    [string]$RelayExe,
    [string]$Pythonw,
    [string]$Relay
)

$ErrorActionPreference = "Stop"

# Codex 的 Windows command hook 会先创建一个 pwsh 控制台。
# 这个脚本在同一个进程内隐藏控制台，避免每次提交提示词时闪出黑窗口。
# 新安装使用 relay exe；旧 Codex 会话可能仍缓存 -Pythonw/-Relay 参数。
# 两种参数都兼容，避免用户更新后、重启 Codex 前 hook 立即失败。
$newLine = [Environment]::NewLine
$windowApiSource = 'using System;' + $newLine +
    'using System.Runtime.InteropServices;' + $newLine +
    'public static class CodexHookWindow {' + $newLine +
    '    [DllImport("kernel32.dll")]' + $newLine +
    '    public static extern IntPtr GetConsoleWindow();' + $newLine +
    '    [DllImport("user32.dll")]' + $newLine +
    '    public static extern bool ShowWindow(IntPtr handle, int command);' + $newLine +
    '}'
Add-Type -TypeDefinition $windowApiSource

$consoleHandle = [CodexHookWindow]::GetConsoleWindow()
if ($consoleHandle -ne [IntPtr]::Zero) {
    # SW_HIDE = 0。只隐藏当前 hook 控制台，不影响 Codex 主窗口。
    [CodexHookWindow]::ShowWindow($consoleHandle, 0) | Out-Null
}

if ($RelayExe) {
    & $RelayExe
    exit $LASTEXITCODE
}

if ($Pythonw -and $Relay) {
    & $Pythonw $Relay
    exit $LASTEXITCODE
}

throw "RelayExe is required."

