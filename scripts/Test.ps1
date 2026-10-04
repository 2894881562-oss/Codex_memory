[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$MemoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$MemoryPython = Join-Path $MemoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $MemoryPython -PathType Leaf)) { throw 'Run .\scripts\Setup.ps1 first.' }
Push-Location $MemoryRoot
try {
    & $MemoryPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; temporary evidence directories are retained.' }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        foreach ($MemoryScript in @('content.js', 'worker.js', 'options.js', 'popup.js')) {
            & node --check (Join-Path $MemoryRoot "browser-extension\$MemoryScript")
            if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax validation failed: $MemoryScript" }
        }
    } else {
        Write-Host 'Node.js unavailable; JS syntax checks skipped. Browser installation still requires manual verification.'
    }
    Write-Host 'Automated checks passed. See docs\ACCEPTANCE.md for real browser / second-account acceptance.'
} finally {
    Pop-Location
}
