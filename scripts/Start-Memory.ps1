[CmdletBinding()]
param(
    [string]$DataDir = '',
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [ValidateRange(1, 3600)][int]$SyncInterval = 3,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$MemoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$MemoryPython = Join-Path $MemoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $MemoryPython -PathType Leaf)) { throw 'Run .\scripts\Setup.ps1 first.' }
if (-not $DataDir) { $DataDir = Join-Path $MemoryRoot 'data' }
$MemoryArgs = @('-m', 'codex_memory', '--data-dir', $DataDir, 'serve', '--host', '127.0.0.1', '--port', "$Port", '--sync-interval', "$SyncInterval")
if (-not $NoBrowser) { $MemoryArgs += '--open-browser' }
Write-Host "Memory vault: $DataDir"
Write-Host "Dashboard: http://127.0.0.1:$Port"
Write-Host 'Keep this window open. Ctrl+C stops the local service.'
& $MemoryPython @MemoryArgs
if ($LASTEXITCODE -ne 0) { throw "Local memory server exited with code $LASTEXITCODE." }
