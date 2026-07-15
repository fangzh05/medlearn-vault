[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Wheel,
    [Parameter(Mandatory = $true)] [string] $InstallRoot,
    [switch] $Json
)

# This file deliberately lives outside the replaceable venv.  It never writes
# child output to the console: callers get exactly one small, ASCII-only JSON
# document, suitable for automation logs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Result([hashtable] $Result) {
    Write-Output ($Result | ConvertTo-Json -Compress)
}

function Assert-AbsoluteLocalPath([string] $Value, [string] $Code) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '^[A-Za-z][A-Za-z0-9+.-]*://') {
        throw $Code
    }
    if (-not [IO.Path]::IsPathRooted($Value)) { throw $Code }
}

function Invoke-Quiet([string] $File, [string[]] $Arguments, [string] $Code) {
    & $File @Arguments 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw $Code }
}

$tempVenv = $null
$previousRoot = $env:MEDLEARN_SYNC_INSTALL_ROOT
$cleanup = 'clean'
$successResult = $null
try {
    Assert-AbsoluteLocalPath $Wheel 'SYNC_INSTALL_ARTIFACT_INVALID'
    Assert-AbsoluteLocalPath $InstallRoot 'SYNC_INSTALL_LOCATION_INVALID'
    $wheelPath = (Resolve-Path -LiteralPath $Wheel -ErrorAction Stop).ProviderPath
    if (-not $wheelPath.EndsWith('.whl', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'SYNC_INSTALL_ARTIFACT_INVALID'
    }
    $wheelhouse = Split-Path -Parent $wheelPath
    if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
        throw 'SYNC_INSTALL_ARTIFACT_INVALID'
    }
    $installRootPath = [IO.Path]::GetFullPath($InstallRoot)
    $tempVenv = Join-Path ([IO.Path]::GetTempPath()) ('medlearn-bootstrap-' + [Guid]::NewGuid().ToString('N'))
    $tempPython = Join-Path $tempVenv 'Scripts\python.exe'
    $tempClient = Join-Path $tempVenv 'Scripts\medlearn.exe'

    Invoke-Quiet 'python.exe' @('-m', 'venv', $tempVenv) 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $tempPython @('-m', 'pip', '--isolated', 'install', '--no-index', '--no-cache-dir', '--find-links', $wheelhouse, $wheelPath) 'SYNC_INSTALL_ARTIFACT_INVALID'
    Invoke-Quiet $tempClient @('--version') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $tempClient @('--help') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $tempClient @('doctor') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $tempPython @('-m', 'pip', 'check') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'

    $env:MEDLEARN_SYNC_INSTALL_ROOT = $installRootPath
    $childOutput = & $tempClient sync install-windows --wheel $wheelPath --json 2>&1
    if ($LASTEXITCODE -ne 0) {
        $childText = ($childOutput | Out-String).Trim()
        try { $child = $childText | ConvertFrom-Json -ErrorAction Stop } catch { $child = $null }
        if ($null -ne $child -and $child.error_code -match '^SYNC_[A-Z0-9_]+$') { throw $child.error_code }
        throw 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    }
    $childText = ($childOutput | Out-String).Trim()
    try { $child = $childText | ConvertFrom-Json -ErrorAction Stop } catch { throw 'SYNC_INSTALL_BOOTSTRAP_FAILURE' }
    if ($null -eq $child -or $child.status -notin @('installed', 'reused')) { throw 'SYNC_INSTALL_BOOTSTRAP_FAILURE' }

    $stableClient = Join-Path $installRootPath 'venv\Scripts\medlearn.exe'
    $stablePython = Join-Path $installRootPath 'venv\Scripts\python.exe'
    Invoke-Quiet $stableClient @('--version') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $stableClient @('--help') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $stableClient @('doctor') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'
    Invoke-Quiet $stablePython @('-m', 'pip', 'check') 'SYNC_INSTALL_BOOTSTRAP_FAILURE'

    $durable = Join-Path $installRootPath 'install-windows-client.ps1'
    $copyTemp = $durable + '.tmp'
    Copy-Item -LiteralPath $PSCommandPath -Destination $copyTemp -Force
    Move-Item -LiteralPath $copyTemp -Destination $durable -Force
    $successResult = @{ status = [string]$child.status }
}
catch {
    $code = [string]$_.Exception.Message
    if ($code -notmatch '^SYNC_[A-Z0-9_]+$') { $code = 'SYNC_INSTALL_BOOTSTRAP_FAILURE' }
    Write-Result @{ status = 'error'; error_code = $code }
    exit 1
}
finally {
    $env:MEDLEARN_SYNC_INSTALL_ROOT = $previousRoot
    if ($null -ne $tempVenv -and (Test-Path -LiteralPath $tempVenv)) {
        try { Remove-Item -LiteralPath $tempVenv -Recurse -Force -ErrorAction Stop } catch { $cleanup = 'warning' }
    }
}
if ($null -ne $successResult) {
    $successResult.cleanup = $cleanup
    Write-Result $successResult
    exit 0
}
