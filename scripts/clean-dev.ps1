$ErrorActionPreference = "Stop"

if (-not $PSScriptRoot) {
    throw "Unable to resolve repository root from script location."
}

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root "pyproject.toml"))) {
    throw "Refusing to clean: pyproject.toml not found in $root."
}

$sourceDirs = @(
    (Join-Path $root "src"),
    (Join-Path $root "tests"),
    (Join-Path $root "scripts")
)
foreach ($dir in $sourceDirs) {
    if (-not (Test-Path -LiteralPath $dir)) {
        continue
    }
    Get-ChildItem -LiteralPath $dir -Recurse -Directory -Filter "__pycache__" |
        Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force
        }
}

$cacheNames = @(".pytest_cache", "build", "dist", "test-results", "playwright-report")
foreach ($name in $cacheNames) {
    $target = Join-Path $root $name
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

Write-Host "Development caches cleaned."
