<#
.SYNOPSIS
    Safe operator-facing Windows release entrypoint for MedLearn.

.DESCRIPTION
    Builds an offline wheel, upgrades through the external bootstrap, validates
    and deploys the Worker, publishes the presentation, and runs isolated
    acceptance. It never accepts or prints credentials. Production changes are
    protected by an exact interactive confirmation; -ValidateOnly has no side
    effects and -SkipProduction stops before the production Vault.
#>
[CmdletBinding()]
param([switch] $ValidateOnly, [switch] $SkipProduction)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Fail([string] $Code) { throw $Code }
function Invoke-Checked([string] $File, [string[]] $Arguments, [string] $Code) {
    & $File @Arguments 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail $Code }
}
function Report([string] $Status, [string] $Phase) { [ordered]@{status=$Status;phase=$Phase}|ConvertTo-Json -Compress }

$repo = Split-Path -Parent $PSScriptRoot
$wheelhouse = Join-Path $repo 'dist\wheelhouse'
$installRoot = Join-Path $env:LOCALAPPDATA 'MedLearn\sync-client'
$bootstrap = Join-Path $PSScriptRoot 'install_windows_client.ps1'
$acceptance = Join-Path $PSScriptRoot 'acceptance\windows_sync_rollout.ps1'
if ($ValidateOnly) {
    foreach ($path in @($bootstrap, $acceptance, (Join-Path $repo 'worker\package.json'))) { if (-not (Test-Path -LiteralPath $path)) { Fail 'SYNC_RELEASE_PREFLIGHT_FAILED' } }
    if ($MyInvocation.MyCommand.Parameters.Keys | Where-Object { $_ -match '(?i)token|secret|password' }) { Fail 'SYNC_RELEASE_SECRET_ARGUMENT_FORBIDDEN' }
    Report 'validated' 'preflight'; exit 0
}
$releaseRoot = $null
try {
    if (-not (Test-Path -LiteralPath $wheelhouse)) { Fail 'SYNC_RELEASE_WHEELHOUSE_MISSING' }
    $releaseRoot = Join-Path ([IO.Path]::GetTempPath()) ('medlearn-release-' + [Guid]::NewGuid().ToString('N'))
    $releaseWheelhouse = Join-Path $releaseRoot 'wheelhouse'; New-Item -ItemType Directory -Path $releaseWheelhouse -Force | Out-Null
    Get-ChildItem -LiteralPath $wheelhouse -File | Copy-Item -Destination $releaseWheelhouse -Force
    Get-ChildItem -LiteralPath $releaseWheelhouse -Filter 'medlearn_vault-*.whl' | Remove-Item -Force
    Invoke-Checked 'python.exe' @('-m','pip','wheel','--no-index','--find-links',$wheelhouse,'--no-build-isolation','--no-deps','--wheel-dir',$releaseWheelhouse,$repo) 'SYNC_RELEASE_WHEEL_BUILD_FAILED'
    $wheel = (Get-ChildItem -LiteralPath $releaseWheelhouse -Filter 'medlearn_vault-*.whl' | Select-Object -First 1).FullName
    if (-not $wheel) { Fail 'SYNC_RELEASE_WHEEL_BUILD_FAILED' }
    Invoke-Checked 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$bootstrap,'-Wheel',$wheel,'-InstallRoot',$installRoot,'-Json') 'SYNC_RELEASE_CLIENT_UPGRADE_FAILED'
    $client = Join-Path $installRoot 'venv\Scripts\medlearn.exe'; $python = Join-Path $installRoot 'venv\Scripts\python.exe'
    Invoke-Checked $client @('--help') 'SYNC_RELEASE_CLIENT_VERIFY_FAILED'; Invoke-Checked $client @('doctor') 'SYNC_RELEASE_CLIENT_VERIFY_FAILED'; Invoke-Checked $python @('-m','pip','check') 'SYNC_RELEASE_CLIENT_VERIFY_FAILED'
    Push-Location (Join-Path $repo 'worker')
    try { foreach ($command in @(@('npm.cmd','ci'),@('npm.cmd','run','lint'),@('npm.cmd','run','typecheck'),@('npm.cmd','test'),@('npm.cmd','run','contracts:check'),@('npx.cmd','--no-install','wrangler','deploy','--dry-run'),@('npm.cmd','run','deploy'))) { Invoke-Checked $command[0] $command[1..($command.Length-1)] 'SYNC_RELEASE_WORKER_FAILED' } } finally { Pop-Location }
    Invoke-Checked 'gh.exe' @('workflow','run','MedLearn Publish Presentation','--ref','main','-f','bundle_path=examples/copd','-f','confirmation=publish-presentation') 'SYNC_RELEASE_PRESENTATION_FAILED'
    Invoke-Checked 'powershell.exe' @('-NoProfile','-File',$acceptance,'-Endpoint','https://medlearn-cloud.fzh050531.workers.dev','-Wheel',$wheel) 'SYNC_RELEASE_TEMP_ACCEPTANCE_FAILED'
    if (-not $SkipProduction) {
        if ((Read-Host 'Type MIGRATE_PRODUCTION to migrate the production Vault') -cne 'MIGRATE_PRODUCTION') { Fail 'SYNC_RELEASE_PRODUCTION_NOT_CONFIRMED' }
        Invoke-Checked $client @('sync','pull','--dry-run','--json') 'SYNC_RELEASE_PRODUCTION_DRY_RUN_FAILED'; Invoke-Checked $client @('sync','pull','--confirm-first-pull','--json') 'SYNC_RELEASE_PRODUCTION_PULL_FAILED'; Invoke-Checked $client @('sync','schedule','install','--interval-minutes','15') 'SYNC_RELEASE_SCHEDULE_FAILED'
    }
    Report 'completed' $(if ($SkipProduction) {'pre-production'}else{'production'})
} catch { $code=[string]$_.Exception.Message; if($code -notmatch '^SYNC_RELEASE_[A-Z0-9_]+$'){$code='SYNC_RELEASE_FAILED'}; Report 'error' $code; exit 1 }
finally { if($releaseRoot -and(Test-Path -LiteralPath $releaseRoot)){Remove-Item -LiteralPath $releaseRoot -Recurse -Force -ErrorAction SilentlyContinue} }
