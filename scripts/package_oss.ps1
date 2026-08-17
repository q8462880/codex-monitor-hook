param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$StageRoot = Join-Path $RepoRoot ".tmp-oss-package"
$PackageRoot = Join-Path $StageRoot "package"
if (-not $OutputPath) {
    $OutputPath = Join-Path $RepoRoot "codex-monitor-hook.zip"
}

function Copy-PackageFiles {
    $Directories = @(".codex", "references", "scripts", "tests")
    foreach ($Directory in $Directories) {
        New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot $Directory) | Out-Null
    }

    $RootFiles = @(
        "AGENTS.md",
        "README.md",
        "INSTALL.md",
        "HOOKS.md",
        "SKILL.md",
        "codex-monitor-hook-install.md",
        "requirements.txt"
    )
    foreach ($File in $RootFiles) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot $File) -Destination $PackageRoot
    }

    Copy-Item -LiteralPath (Join-Path $RepoRoot ".codex\INSTALL.md") -Destination (Join-Path $PackageRoot ".codex")
    Copy-Item -LiteralPath (Join-Path $RepoRoot "references\codex_config_hooks.toml") -Destination (Join-Path $PackageRoot "references")
    Get-ChildItem -LiteralPath (Join-Path $RepoRoot "scripts") -File -Filter "*.py" |
        Copy-Item -Destination (Join-Path $PackageRoot "scripts")
    Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\install.ps1") -Destination (Join-Path $PackageRoot "scripts")
    Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\install.sh") -Destination (Join-Path $PackageRoot "scripts")
    Get-ChildItem -LiteralPath (Join-Path $RepoRoot "tests") -File -Filter "*.py" |
        Copy-Item -Destination (Join-Path $PackageRoot "tests")
}

function Assert-CleanArchive {
    param([string]$ArchivePath)

    $Entries = @(tar -tf $ArchivePath)
    $Forbidden = $Entries | Where-Object {
        $_ -match "(^|/)(\.git|__pycache__|build|dist|\.tmp)" -or
        $_ -match "\.(exe|pyc)$" -or
        $_ -match "codex_hook_windows_launcher\.ps1$"
    }
    if ($Forbidden) {
        throw "Package contains forbidden release entries: $($Forbidden -join ', ')"
    }
    foreach ($Required in @(".codex/INSTALL.md", "scripts/install.ps1", "scripts/install.sh", "SKILL.md")) {
        if ($Entries -notcontains $Required) {
            throw "Package is missing $Required"
        }
    }
    return $Entries
}

if (Test-Path -LiteralPath $StageRoot) {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
Copy-PackageFiles

if (Test-Path -LiteralPath $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}
Compress-Archive `
    -Path (Get-ChildItem -LiteralPath $PackageRoot -Force | ForEach-Object FullName) `
    -DestinationPath $OutputPath `
    -CompressionLevel Optimal `
    -Force

$Entries = Assert-CleanArchive $OutputPath
$Hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
$HashPath = "$OutputPath.sha256"
"$Hash  $(Split-Path -Leaf $OutputPath)" | Set-Content -LiteralPath $HashPath -Encoding ascii
Remove-Item -LiteralPath $StageRoot -Recurse -Force

Write-Output "PACKAGE=$OutputPath"
Write-Output "SHA256=$Hash"
Write-Output "FILES=$($Entries.Count)"
Write-Output "SIZE=$((Get-Item -LiteralPath $OutputPath).Length)"
