[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$CoreOnly
)
$ErrorActionPreference = 'Stop'
$MemoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$MemoryVenvPython = Join-Path $MemoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $MemoryVenvPython -PathType Leaf)) {
    $MemoryPrefix = @()
    if ($Python) {
        $MemoryLauncher = $Python
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $MemoryLauncher = (Get-Command py).Source
        $MemoryPrefix = @('-3')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $MemoryLauncher = (Get-Command python).Source
    } else {
        throw 'Python 3.11+ was not found. Install Python or run Setup.ps1 -Python <absolute-python-path>.'
    }
    & $MemoryLauncher @MemoryPrefix -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    & $MemoryLauncher @MemoryPrefix -m venv (Join-Path $MemoryRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Creating the project virtual environment failed.' }
}
& $MemoryVenvPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Existing .venv needs Python 3.11+. Select a supported environment manually; no files were removed.' }
$MemoryInstallTarget = if ($CoreOnly) { $MemoryRoot } else { "$MemoryRoot[all]" }
& $MemoryVenvPython -m pip install -e $MemoryInstallTarget
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Review pip output; your archive was not changed.' }
Write-Host "Ready: $MemoryVenvPython"
Write-Host 'Next: .\scripts\Start-Memory.ps1'
