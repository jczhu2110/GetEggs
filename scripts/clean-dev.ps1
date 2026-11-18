$ErrorActionPreference = "SilentlyContinue"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $sourceDirs = @("$root\src", "$root\tests", "$root\scripts")
    Get-ChildItem -Path $sourceDirs -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force

    Remove-Item -Recurse -Force ".pytest_cache", "build", "dist", "test-results", "playwright-report"
    Write-Host "Development caches cleaned."
} finally {
    Pop-Location
}
