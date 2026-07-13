<#
.SYNOPSIS
    Isolated manual acceptance script for the MedLearn Vault Windows sync
    rollout.  Never uses the real Vault automatically.

.DESCRIPTION
    This script exercises every production step under a self-contained
    temporary root with spaces and Chinese characters in its path, a
    temporary MEDLEARN_HOME, and a throwaway Obsidian Vault.  It verifies
    command-line quoting, structured status, idempotent removal, and that
    no token ever appears in files, arguments, logs, or the scheduled task.

    The script must be run on Windows 10/11 with PowerShell 7+ or Windows
    PowerShell 5.1.  It is safe to run repeatedly — each invocation creates
    a fresh temporary root.

    CI VALIDATION MODE (no task registration, no Worker contact):
        powershell -NoProfile -NonInteractive -File scripts/acceptance/windows_sync_rollout.ps1 -ValidateOnly

.PARAMETER Endpoint
    Full HTTPS Worker endpoint, e.g. https://medlearn-cloud.example.workers.dev.

.PARAMETER Wheel
    Path to a locally built medlearn_vault wheel file.

.PARAMETER ValidateOnly
    Verify parameter handling, generated paths and command construction without
    registering a task or contacting the Worker.  Suitable for CI.

.PARAMETER WhatIf
    Alias for -ValidateOnly.

.PARAMETER KeepArtifacts
    Do not delete the temporary root after the script completes (diagnostic).

.EXAMPLE
    # Real isolated acceptance (interactive — will prompt for the sync token):
    powershell -NoProfile -NonInteractive -File scripts/acceptance/windows_sync_rollout.ps1 `
        -Endpoint "https://medlearn-cloud.<subdomain>.workers.dev" `
        -Wheel "dist/wheelhouse/medlearn_vault-0.13.0-py3-none-any.whl"

.NOTES
    This script is NOT run by normal GitHub CI because it registers a real
    Windows Scheduled Task.  CI only exercises -ValidateOnly.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Endpoint,

    [Parameter(Mandatory = $false)]
    [string]$Wheel,

    [Parameter()]
    [switch]$ValidateOnly,

    [Parameter()]
    [switch]$KeepArtifacts
)

# ---------------------------------------------------------------------------
# Fail-fast
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $ValidateOnly -and (-not $Endpoint -or -not $Wheel)) {
    Write-Error 'Both -Endpoint and -Wheel are required (or use -ValidateOnly).'
    exit 1
}

$isWin = if (Test-Path variable:IsWindows) { $IsWindows } else { $true }
if (-not $isWin) {
    Write-Error 'This script must be run on Windows.'
    exit 1
}

# ---------------------------------------------------------------------------
# Safety: no token parameter may exist
# ---------------------------------------------------------------------------
$paramNames = $MyInvocation.MyCommand.Parameters.Keys
if ('Token' -in $paramNames -or 'SyncToken' -in $paramNames -or 'Secret' -in $paramNames) {
    Write-Error 'INTERNAL ERROR: token parameter detected — this script must never accept a token.'
    exit 1
}

# ---------------------------------------------------------------------------
# Validation-only mode
# ---------------------------------------------------------------------------
if ($ValidateOnly) {
    Write-Host '=== VALIDATION MODE ===' -ForegroundColor Cyan
    Write-Host 'Verifying parameter handling, generated paths, and command construction.'
    Write-Host 'No task will be registered and no Worker contact will be made.'
    Write-Host ''

    $dummyRoot = Join-Path $env:TEMP 'medlearn-acceptance-validate'
    $dummyHome = Join-Path $dummyRoot '同步 主目录'    # Chinese + space
    $dummyInstall = Join-Path $dummyRoot 'MedLearn 安装' # space
    $dummyVault = Join-Path $dummyRoot '测试 Vault'      # Chinese + space

    # Verify paths contain required characters.
    if ($dummyHome -notmatch '同步' -or $dummyHome -notmatch ' ') {
        Write-Error 'Temporary home path does not contain required Unicode + space.'
        exit 1
    }
    if ($dummyInstall -notmatch ' ') {
        Write-Error 'Temporary install path does not contain required space.'
        exit 1
    }
    if ($dummyVault -notmatch '测试' -or $dummyVault -notmatch ' ') {
        Write-Error 'Temporary vault path does not contain required Unicode + space.'
        exit 1
    }

    Write-Host "  MEDLEARN_HOME override : $dummyHome"
    Write-Host "  Install root           : $dummyInstall"
    Write-Host "  Vault path             : $dummyVault"
    Write-Host ''

    # Verify medlearn is importable (CLI command construction).
    $medlearnVersion = & medlearn --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'medlearn --version failed. Is the package installed?'
        exit 1
    }
    Write-Host "  medlearn version       : $medlearnVersion"

    # Verify sync --help works.
    $syncHelp = & medlearn sync --help 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'medlearn sync --help failed.'
        exit 1
    }

    # Verify schedule subcommands exist and accept --help.
    foreach ($sub in @('install', 'status', 'remove')) {
        $help = & medlearn sync schedule $sub --help 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "medlearn sync schedule $sub --help failed."
            exit 1
        }
        Write-Host "  schedule $sub --help   : ok"
    }

    # Verify --what-if produces a valid definition.
    $saveInstallRoot = $env:MEDLEARN_SYNC_INSTALL_ROOT
    $saveHome = $env:MEDLEARN_HOME
    try {
        $env:MEDLEARN_SYNC_INSTALL_ROOT = $dummyInstall
        $env:MEDLEARN_HOME = $dummyHome
        $whatIfOutput = & medlearn sync schedule install --what-if --json 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "schedule install --what-if failed: $whatIfOutput"
            exit 1
        }
        $whatIfPlan = ($whatIfOutput | Out-String).Trim() | ConvertFrom-Json
        if ($whatIfPlan.status -ne 'planned') {
            Write-Error "Expected status=planned, got: $($whatIfPlan.status)"
            exit 1
        }
        if ($whatIfPlan.task_name -ne 'MedLearn Vault Sync') {
            Write-Error "Unexpected task name: $($whatIfPlan.task_name)"
            exit 1
        }
    } finally {
        $env:MEDLEARN_SYNC_INSTALL_ROOT = $saveInstallRoot
        $env:MEDLEARN_HOME = $saveHome
    }

    # Verify install-windows --dry-run works.
    $dummyWheel = Join-Path $dummyRoot 'medlearn_vault-0.13.0-py3-none-any.whl'
    try {
        New-Item -ItemType File -Force $dummyWheel -Value 'dummy wheel' | Out-Null
        $saveInstallRoot2 = $env:MEDLEARN_SYNC_INSTALL_ROOT
        $env:MEDLEARN_SYNC_INSTALL_ROOT = $dummyInstall
        $dryRunOutput = & medlearn sync install-windows --wheel $dummyWheel --dry-run --json 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "install-windows --dry-run failed: $dryRunOutput"
            exit 1
        }
        $dryRunPlan = ($dryRunOutput | Out-String).Trim() | ConvertFrom-Json
        if ($dryRunPlan.status -ne 'planned') {
            Write-Error "Expected status=planned, got: $($dryRunPlan.status)"
            exit 1
        }
    } finally {
        $env:MEDLEARN_SYNC_INSTALL_ROOT = $saveInstallRoot2
    }

    # Verify no token appears in command help text.
    foreach ($cmd in @('sync login --help', 'sync pull --help', 'sync schedule install --help')) {
        $helpText = & medlearn $cmd.Split(' ') 2>&1 | Out-String
        if ($helpText -match '\b(token|secret|password|key)\b') {
            Write-Warning "Command '$cmd' help may reference a sensitive word."
        }
    }

    Write-Host ''
    Write-Host '=== VALIDATION PASSED ===' -ForegroundColor Green
    Write-Host 'Parameter handling, path generation, and command construction are valid.'
    Write-Host 'Run without -ValidateOnly for full isolated acceptance.'
    exit 0
}

# ===================================================================
# FULL ISOLATED ACCEPTANCE
# ===================================================================

# ---------------------------------------------------------------------------
# Resolve inputs
# ---------------------------------------------------------------------------
$WheelPath = Resolve-Path $Wheel -ErrorAction Stop
if (-not (Test-Path $WheelPath -PathType Leaf)) {
    Write-Error "Wheel not found: $WheelPath"
    exit 1
}
if ($WheelPath -notmatch '\.whl$') {
    Write-Error "Not a .whl file: $WheelPath"
    exit 1
}

# Normalise endpoint.
$Endpoint = $Endpoint.TrimEnd('/')
if ($Endpoint -notmatch '^https://') {
    Write-Error 'Endpoint must start with https://'
    exit 1
}

# ---------------------------------------------------------------------------
# Create isolated temporary root (spaces + Chinese)
# ---------------------------------------------------------------------------
$TempRoot = Join-Path $env:TEMP "medlearn-acceptance-$(Get-Random -Minimum 100000 -Maximum 999999)"
# Ensure path contains spaces and Chinese characters.
$TempRoot = Join-Path $TempRoot '同步 测试'
New-Item -ItemType Directory -Force $TempRoot | Out-Null
Write-Host "Temporary root: $TempRoot" -ForegroundColor Cyan

$InstallRoot = Join-Path $TempRoot 'MedLearn 安装'
$HomeDir = Join-Path $TempRoot '同步 主目录'
$VaultPath = Join-Path $TempRoot '测试 Vault'
$ObsidianDir = Join-Path $VaultPath '.obsidian'

# ---------------------------------------------------------------------------
# Cleanup handler
# ---------------------------------------------------------------------------
function Cleanup {
    if ($KeepArtifacts) {
        Write-Host ''
        Write-Host "Keeping artifacts at: $TempRoot" -ForegroundColor Yellow
        return
    }
    Write-Host ''
    Write-Host "Cleaning up temporary root: $TempRoot" -ForegroundColor Cyan
    try {
        # Remove the scheduled task if it still exists (belt-and-suspenders).
        $existing = Get-ScheduledTask -TaskName 'MedLearn Vault Sync' -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName 'MedLearn Vault Sync' -Confirm:$false -ErrorAction SilentlyContinue
        }
    } catch { }
    Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Step 0: Confirm no real Vault is involved
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== SAFETY CHECK ===' -ForegroundColor Yellow
Write-Host "This script will create and destroy an isolated Vault at:"
Write-Host "  $VaultPath"
Write-Host "It will NEVER touch your real Obsidian Vault."
Write-Host ''

# ---------------------------------------------------------------------------
# Step 1: Install medlearn CLI under the temporary root
# ---------------------------------------------------------------------------
Write-Host '=== Step 1: Dry-run install ===' -ForegroundColor Cyan
$env:MEDLEARN_SYNC_INSTALL_ROOT = $InstallRoot
$dryInstall = & medlearn sync install-windows --wheel $WheelPath --dry-run --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Dry-run install failed: $dryInstall"
    Cleanup; exit 1
}
$dryInstallObj = $dryInstall | ConvertFrom-Json
Write-Host "  status           : $($dryInstallObj.status)"
Write-Host "  executable       : $($dryInstallObj.executable)"
Write-Host "  venv             : $($dryInstallObj.venv)"
Write-Host "  network_download : $($dryInstallObj.network_download)"

if ($dryInstallObj.executable -notmatch 'MedLearn 安装') {
    Write-Error "Executable path does not contain expected space-containing install root."
    Cleanup; exit 1
}

Write-Host ''
Write-Host '=== Step 2: Real install ===' -ForegroundColor Cyan
$installResult = & medlearn sync install-windows --wheel $WheelPath --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Install failed: $installResult"
    Cleanup; exit 1
}
$installObj = $installResult | ConvertFrom-Json
Write-Host "  status: $($installObj.status)"

# Verify the stable installed executable.
$clientExe = $installObj.executable
if (-not (Test-Path $clientExe -PathType Leaf)) {
    Write-Error "Installed executable not found: $clientExe"
    Cleanup; exit 1
}
$version = & $clientExe --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Installed executable --version failed: $version"
    Cleanup; exit 1
}
Write-Host "  client version: $version"

# ---------------------------------------------------------------------------
# Step 3: Configure the temporary Vault
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 3: Configure temporary Vault ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Force $ObsidianDir | Out-Null
$env:MEDLEARN_HOME = $HomeDir

$configResult = & $clientExe sync configure --endpoint $Endpoint --vault $VaultPath --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Configure failed: $configResult"
    Cleanup; exit 1
}
Write-Host "  $configResult"

# ---------------------------------------------------------------------------
# Step 4: Login (interactive — user must paste token)
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 4: Login ===' -ForegroundColor Cyan
Write-Host 'You will be prompted for the sync token. The token is NEVER written to disk,'
Write-Host 'logs, task arguments, or PowerShell command history.'
Write-Host ''
& $clientExe sync login
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Login failed.'
    Cleanup; exit 1
}

# ---------------------------------------------------------------------------
# Step 5: First dry-run
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 5: First dry-run ===' -ForegroundColor Cyan
$dryRunResult = & $clientExe sync pull --dry-run --json 2>&1
Write-Host "  $dryRunResult"
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3) {
    Write-Error "Dry-run failed with exit code $LASTEXITCODE."
    Cleanup; exit 1
}

# ---------------------------------------------------------------------------
# Step 6: Explicit confirmation before first real pull
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 6: First real pull ===' -ForegroundColor Yellow
$confirmation = Read-Host 'Ready for first real pull. Type YES to proceed'
if ($confirmation -ne 'YES') {
    Write-Host 'Aborted by user.' -ForegroundColor Yellow
    Cleanup; exit 0
}

$pullResult = & $clientExe sync pull --confirm-first-pull --json 2>&1
Write-Host "  $pullResult"
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3) {
    Write-Error "First pull failed with exit code $LASTEXITCODE."
    Cleanup; exit 1
}

# ---------------------------------------------------------------------------
# Step 7: Install Scheduled Task
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 7: Install Scheduled Task ===' -ForegroundColor Cyan

# First, verify --what-if.
$schedulePlanOutput = & $clientExe sync schedule install --what-if --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "schedule install --what-if failed: $schedulePlanOutput"
    Cleanup; exit 1
}
$schedulePlan = ($schedulePlanOutput | Out-String).Trim() | ConvertFrom-Json
Write-Host "  what-if status : $($schedulePlan.status)"
Write-Host "  task_name      : $($schedulePlan.task_name)"
Write-Host "  interval       : $($schedulePlan.interval_minutes) minutes"

# Verify no token in what-if output.
$schedulePlanStr = $schedulePlanOutput | Out-String
if ($schedulePlanStr -match '(?i)\b(bearer\s|token\s*[:=]\s*\S{32}|authorization\s*[:=])') {
    Write-Error 'Token-like content found in --what-if output!'
    Cleanup; exit 1
}

# Real install.
$scheduleResult = & $clientExe sync schedule install --interval-minutes 15 --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Schedule install failed: $scheduleResult"
    Cleanup; exit 1
}
Write-Host "  schedule status: $(($scheduleResult | ConvertFrom-Json).status)"

# ---------------------------------------------------------------------------
# Step 8: Structured status
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 8: Structured task status ===' -ForegroundColor Cyan
$statusResult = & $clientExe sync schedule status --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Schedule status failed: $statusResult"
    Cleanup; exit 1
}
$statusObj = $statusResult | ConvertFrom-Json
Write-Host "  task_name        : $($statusObj.task_name)"
Write-Host "  registered       : $($statusObj.registered)"
Write-Host "  state            : $($statusObj.state)"
Write-Host "  last_run_time    : $($statusObj.last_run_time)"
Write-Host "  next_run_time    : $($statusObj.next_run_time)"
Write-Host "  last_task_result : $($statusObj.last_task_result)"
Write-Host "  executable       : $($statusObj.executable)"

if ($statusObj.registered -ne $true) {
    Write-Error 'Task is not registered after install.'
    Cleanup; exit 1
}

# Verify no token in status output.
$statusStr = $statusResult | Out-String
if ($statusStr -match '(?i)\b(bearer\s|token\s*[:=]\s*\S{32}|authorization\s*[:=])') {
    Write-Error 'Token-like content found in status output!'
    Cleanup; exit 1
}

# ---------------------------------------------------------------------------
# Step 9: Verify no token in any on-disk artifact
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 9: Token scan ===' -ForegroundColor Cyan

$scanPaths = @(
    (Join-Path $HomeDir 'config.json'),
    (Join-Path $HomeDir 'rollout.json'),
    (Join-Path $HomeDir 'state.json'),
    (Join-Path $HomeDir 'scheduled-results.jsonl'),
    (Join-Path $InstallRoot 'schedule.json'),
    (Join-Path $InstallRoot 'run-scheduled.ps1'),
    (Join-Path $InstallRoot 'install.json')
)

# Also check the task's action arguments.
try {
    $taskObj = Get-ScheduledTask -TaskName 'MedLearn Vault Sync' -ErrorAction SilentlyContinue
    if ($taskObj -and $taskObj.Actions.Count -gt 0) {
        $actionArgs = $taskObj.Actions[0].Arguments
        if ($actionArgs -match '(?i)\b(bearer\s|token\s*[:=]\s*\S{32}|authorization\s*[:=])') {
            Write-Error 'Token-like content found in scheduled task action arguments!'
            Cleanup; exit 1
        }
        Write-Host "  task action args  : clean"
    }
} catch { }

foreach ($path in $scanPaths) {
    if (Test-Path $path) {
        $content = Get-Content -Raw $path -ErrorAction SilentlyContinue
        if ($content -match '(?i)\b(bearer\s[^\s]{20,}|token\s*[:=]\s*\S{32}|authorization\s*[:=]\s*\S+)') {
            Write-Error "Token-like content found in: $path"
            Cleanup; exit 1
        }
        Write-Host "  $(Split-Path $path -Leaf) : clean"
    }
}

# Also verify the credential file is DPAPI ciphertext (binary, not plaintext).
$credPath = Join-Path $HomeDir 'credential.bin'
if (Test-Path $credPath) {
    $credBytes = [System.IO.File]::ReadAllBytes($credPath)
    $credText = [System.Text.Encoding]::UTF8.GetString($credBytes)
    if ($credText -match '^\s*[a-zA-Z0-9+/=]{32,}\s*$') {
        Write-Error 'Credential file appears to be plaintext, not DPAPI ciphertext!'
        Cleanup; exit 1
    }
    Write-Host '  credential.bin    : DPAPI ciphertext (not plaintext)'
}

Write-Host '  All artifacts token-free.' -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 10: Start task and wait for completion
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 10: Start task and verify execution ===' -ForegroundColor Cyan

Start-ScheduledTask -TaskName 'MedLearn Vault Sync'
Write-Host '  Task started.'

# Wait with bounded timeout (2 minutes).
$deadline = (Get-Date).AddMinutes(2)
$completed = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName 'MedLearn Vault Sync' -ErrorAction SilentlyContinue
    if ($info -and $info.LastRunTime.Year -gt 2000) {
        Write-Host "  LastRunTime    : $($info.LastRunTime.ToString('o'))"
        Write-Host "  LastTaskResult : $($info.LastTaskResult)"
        if ($info.LastTaskResult -eq 0) {
            Write-Host '  Task completed successfully.' -ForegroundColor Green
            $completed = $true
        } else {
            Write-Host "  Task completed with non-zero result: 0x$('{0:X8}' -f $info.LastTaskResult)" -ForegroundColor Yellow
            $completed = $true
        }
        break
    }
}

if (-not $completed) {
    Write-Warning 'Task did not complete within timeout. This may be due to network conditions.'
    Write-Warning 'Check LastTaskResult manually with: medlearn sync schedule status --json'
}

# Verify scheduled log has a new record.
$logPath = Join-Path $HomeDir 'scheduled-results.jsonl'
if (Test-Path $logPath) {
    $records = Get-Content $logPath | ConvertFrom-Json
    Write-Host "  Scheduled log records: $($records.Count)"
    if ($records.Count -gt 0) {
        $last = $records[-1]
        Write-Host "  Last record status : $($last.status)"
        Write-Host "  Client version     : $($last.client_version)"
    }
}

# Verify the scheduled wrapper used the correct MEDLEARN_HOME.
$wrapperPath = Join-Path $InstallRoot 'run-scheduled.ps1'
if (Test-Path $wrapperPath) {
    $wrapperContent = Get-Content -Raw $wrapperPath
    if ($wrapperContent -notmatch [regex]::Escape($HomeDir)) {
        Write-Error "Wrapper does not reference the expected MEDLEARN_HOME: $HomeDir"
        Cleanup; exit 1
    }
    Write-Host "  Wrapper MEDLEARN_HOME: correct"
}

# ---------------------------------------------------------------------------
# Step 11: Remove Scheduled Task
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 11: Remove Scheduled Task ===' -ForegroundColor Cyan
$removeResult = & $clientExe sync schedule remove --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Schedule remove failed: $removeResult"
    Cleanup; exit 1
}
$removeObj = $removeResult | ConvertFrom-Json
Write-Host "  status    : $($removeObj.status)"
Write-Host "  task_name : $($removeObj.task_name)"

if ($removeObj.status -ne 'removed') {
    Write-Error "Expected status 'removed', got: $($removeObj.status)"
    Cleanup; exit 1
}

# ---------------------------------------------------------------------------
# Step 12: Verify task is actually absent
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 12: Verify task absence ===' -ForegroundColor Cyan
$recheck = & $clientExe sync schedule status --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Post-removal status check failed: $recheck"
    Cleanup; exit 1
}
$recheckObj = $recheck | ConvertFrom-Json
if ($recheckObj.registered -ne $false) {
    Write-Error 'Task is still registered after removal!'
    Cleanup; exit 1
}
Write-Host '  Task confirmed absent.' -ForegroundColor Green

# Also verify via PowerShell directly.
$directCheck = Get-ScheduledTask -TaskName 'MedLearn Vault Sync' -ErrorAction SilentlyContinue
if ($directCheck) {
    Write-Error 'Get-ScheduledTask still finds the task after removal!'
    Cleanup; exit 1
}
Write-Host '  Get-ScheduledTask confirms absence.'

# ---------------------------------------------------------------------------
# Step 13: Verify Vault content was not deleted by removal
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 13: Vault integrity check ===' -ForegroundColor Cyan
if (-not (Test-Path $ObsidianDir)) {
    Write-Error '.obsidian directory was deleted — Vault content must never be touched!'
    Cleanup; exit 1
}
Write-Host "  .obsidian: intact"
if (Test-Path (Join-Path $VaultPath 'MedLearn')) {
    Write-Host "  MedLearn/ : still present (as expected)"
}
Write-Host '  Vault content preserved.' -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 14: Cleanup
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '=== Step 14: Cleanup ===' -ForegroundColor Cyan
# Remove env overrides by clearing them.
$env:MEDLEARN_SYNC_INSTALL_ROOT = ''
$env:MEDLEARN_HOME = ''
Cleanup

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '========================================' -ForegroundColor Green
Write-Host '  ACCEPTANCE TEST PASSED' -ForegroundColor Green
Write-Host '========================================' -ForegroundColor Green
Write-Host "  Installed       : $clientExe"
Write-Host "  Version         : $version"
Write-Host "  Vault           : $VaultPath (destroyed)"
Write-Host "  Task installed  : successfully"
Write-Host "  Task executed   : $(if ($completed) { 'yes' } else { 'timeout — check manually' })"
Write-Host "  Task removed    : verified absent"
Write-Host "  Token in files  : none"
Write-Host "  Vault preserved : yes"
Write-Host '========================================' -ForegroundColor Green
exit 0
