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
    PowerShell 5.1.

    CI VALIDATION MODE (no task registration, no Worker contact):
        powershell -NoProfile -NonInteractive -File scripts/acceptance/windows_sync_rollout.ps1 -ValidateOnly

    FULL ISOLATED ACCEPTANCE (interactive — prompts for token and pull
    confirmation; do NOT use -NonInteractive):
        powershell -NoProfile -File scripts/acceptance/windows_sync_rollout.ps1 `
            -Endpoint "https://medlearn-cloud.<subdomain>.workers.dev" `
            -Wheel "dist/wheelhouse/medlearn_vault-0.13.0-py3-none-any.whl"

.PARAMETER Endpoint
    Full HTTPS Worker endpoint, e.g. https://medlearn-cloud.example.workers.dev.

.PARAMETER Wheel
    Path to a locally built medlearn_vault wheel file.

.PARAMETER ValidateOnly
    Verify parameter handling, generated paths and command construction without
    registering a task or contacting the Worker.  Suitable for CI.

.PARAMETER KeepArtifacts
    Do not delete the temporary root after the script completes (diagnostic).
    The Scheduled Task is still removed and verified absent.

.NOTES
    This script is NOT run by normal GitHub CI because it registers a real
    Windows Scheduled Task.  CI only exercises -ValidateOnly.
    The full acceptance command must be run interactively (no -NonInteractive)
    because it invokes `medlearn sync login` and a Read-Host confirmation.
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
$Script:ParamNames = $MyInvocation.MyCommand.Parameters.Keys
if ('Token' -in $Script:ParamNames -or 'SyncToken' -in $Script:ParamNames -or 'Secret' -in $Script:ParamNames) {
    Write-Error 'INTERNAL ERROR: token parameter detected — this script must never accept a token.'
    exit 1
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$Script:TaskName = 'MedLearn Vault Sync'
$Script:TaskTimeoutMinutes = 3

# ---------------------------------------------------------------------------
# Helper: check for token-like content in text
# ---------------------------------------------------------------------------
function Test-TokenExposure {
    param([string]$Text, [string]$Label)
    if ($Text -match '(?i)\b(bearer\s[^\s]{20,}|authorization\s*[=:]\s*\S+|token\s*[=:]\s*\S{32,})') {
        Write-Error "Token-like content found in $Label !"
        return $true
    }
    return $false
}

# ---------------------------------------------------------------------------
# Helper: check for token-like content in a file
# ---------------------------------------------------------------------------
function Test-FileTokenExposure {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path $Path)) { return $false }
    try {
        $content = Get-Content -Raw $Path -ErrorAction Stop
        return Test-TokenExposure -Text $content -Label $Label
    } catch {
        Write-Error "Cannot read $Label : $_"
        return $true  # treat unreadable as exposure risk
    }
}

# ---------------------------------------------------------------------------
# Helper: check if the acceptance task is registered (used for pre-flight
# and for conditional cleanup).
# ---------------------------------------------------------------------------
function Test-AcceptanceTaskExists {
    $task = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction SilentlyContinue
    return ($null -ne $task)
}

# ---------------------------------------------------------------------------
# VALIDATION-ONLY MODE
# ---------------------------------------------------------------------------
if ($ValidateOnly) {
    Write-Host '=== VALIDATION MODE ===' -ForegroundColor Cyan
    Write-Host 'Verifying parameter handling, generated paths, and command construction.'
    Write-Host 'No task will be registered and no Worker contact will be made.'
    Write-Host ''

    $dummyRoot = Join-Path $env:TEMP 'medlearn-acceptance-validate'
    $dummyHome = Join-Path $dummyRoot '同步 主目录'
    $dummyInstall = Join-Path $dummyRoot 'MedLearn 安装'
    $dummyVault = Join-Path $dummyRoot '测试 Vault'

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

    $medlearnVersion = & medlearn --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'medlearn --version failed. Is the package installed?'
        exit 1
    }
    Write-Host "  medlearn version       : $medlearnVersion"

    $null = & medlearn sync --help 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Error 'medlearn sync --help failed.'; exit 1 }

    foreach ($sub in @('install', 'status', 'remove')) {
        $null = & medlearn sync schedule $sub --help 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "medlearn sync schedule $sub --help failed."
            exit 1
        }
        Write-Host "  schedule $sub --help   : ok"
    }

    # Verify --what-if
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
        if ($whatIfPlan.task_name -ne $Script:TaskName) {
            Write-Error "Unexpected task name: $($whatIfPlan.task_name)"
            exit 1
        }
    } finally {
        $env:MEDLEARN_SYNC_INSTALL_ROOT = $saveInstallRoot
        $env:MEDLEARN_HOME = $saveHome
    }

    # Verify install-windows --dry-run
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

    # Verify script help text does not contain -NonInteractive on full command
    $selfPath = $MyInvocation.MyCommand.Path
    $selfContent = Get-Content -Raw $selfPath
    # Full command (after .EXAMPLE) must use backtick line continuation, not Unix \
    if ($selfContent -notmatch '\.EXAMPLE[\s\S]*?powershell -NoProfile -File[\s\S]*?-Endpoint') {
        Write-Warning 'Example full acceptance command pattern not found in script help.'
    }
    # Full command must NOT use -NonInteractive
    $exampleSection = if ($selfContent -match '\.EXAMPLE([\s\S]*?)(?=\.PARAMETER|\z)') { $Matches[1] } else { '' }
    if ($exampleSection -match '-NonInteractive') {
        Write-Error 'Example full acceptance command must NOT contain -NonInteractive.'
        exit 1
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
# Gate tracking — each gate must be explicitly set to $true before
# ACCEPTANCE TEST PASSED may be printed.
# ---------------------------------------------------------------------------
$Gate = @{
    Installed               = $false
    ExecutableSmokeTest     = $false
    VaultConfigured         = $false
    LoginCompleted          = $false
    DryRunCompleted         = $false
    FirstPullCompleted      = $false
    TaskPreflightPassed     = $false
    TaskInstalled           = $false
    TaskPrincipalVerified   = $false
    TaskDefinitionVerified  = $false
    TaskStatusValid         = $false
    TaskStarted             = $false
    TaskObserved            = $false
    TaskLastRunTimeAdvanced = $false
    TaskStateNotRunning     = $false
    TaskLastResultZero      = $false
    ScheduledLogExists      = $false
    ScheduledLogNewRecord   = $false
    ScheduledLogRecordValid = $false
    WrapperHomeMatches      = $false
    TokenScanPassed         = $false
    TaskRemoved             = $false
    TaskAbsenceVerified     = $false
    VaultContentPreserved   = $false
    CleanupCompleted        = $false
}

function Set-Gate {
    param([string]$Name)
    $Gate[$Name] = $true
    Write-Host "  [gate] $Name : passed" -ForegroundColor Green
}

function Assert-Gate {
    param([string]$Name, [string]$Message)
    if (-not $Gate[$Name]) {
        Write-Error "GATE FAILED — $Name : $Message"
        exit 2
    }
}

# ---------------------------------------------------------------------------
# Pre-flight: check for pre-existing production task
# ---------------------------------------------------------------------------
Write-Host '=== PRE-FLIGHT: Check for existing task ===' -ForegroundColor Cyan
if (Test-AcceptanceTaskExists) {
    Write-Error @"
A Scheduled Task named '$($Script:TaskName)' already exists on this machine.

This acceptance script will NOT overwrite, modify, or delete an existing
production task.  If this is a leftover from a previous acceptance run,
remove it manually first:

    Unregister-ScheduledTask -TaskName '$($Script:TaskName)' -Confirm:`$false

Then re-run this script.

If this is your production task, do NOT run this acceptance script.
Use a different machine or remove the production task before testing.
"@
    exit 1
}
Set-Gate 'TaskPreflightPassed'

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
$Endpoint = $Endpoint.TrimEnd('/')
if ($Endpoint -notmatch '^https://') {
    Write-Error 'Endpoint must start with https://'
    exit 1
}

# ---------------------------------------------------------------------------
# Create isolated temporary root (spaces + Chinese)
# ---------------------------------------------------------------------------
$TempRoot = Join-Path $env:TEMP "medlearn-acceptance-$(Get-Random -Minimum 100000 -Maximum 999999)"
$TempRoot = Join-Path $TempRoot '同步 测试'
New-Item -ItemType Directory -Force $TempRoot | Out-Null
Write-Host ''
Write-Host "Temporary root: $TempRoot" -ForegroundColor Cyan

$InstallRoot = Join-Path $TempRoot 'MedLearn 安装'
$HomeDir = Join-Path $TempRoot '同步 主目录'
$VaultPath = Join-Path $TempRoot '测试 Vault'
$ObsidianDir = Join-Path $VaultPath '.obsidian'

# Track whether THIS invocation created the task.
$Script:TaskCreatedByThisRun = $false
$Script:TaskInstalledWithElevation = $false

# ---------------------------------------------------------------------------
# Safe emergency task cleanup — only called when we created the task and
# cleanup fails.  Prints recovery commands and retains artifacts.
# ---------------------------------------------------------------------------
function Exit-UnsafeCleanup {
    param([string]$Reason)
    Write-Host ''
    Write-Host '========================================' -ForegroundColor Red
    Write-Host '  ACCEPTANCE CLEANUP FAILED' -ForegroundColor Red
    Write-Host '========================================' -ForegroundColor Red
    Write-Host "  Reason: $Reason"
    Write-Host ''
    Write-Host '  The temporary root is retained for diagnosis:'
    Write-Host "    $TempRoot"
    Write-Host ''
    Write-Host '  If the task is still registered, remove it manually:'
    Write-Host "    Unregister-ScheduledTask -TaskName '$($Script:TaskName)' -Confirm:`$false"
    Write-Host ''
    Write-Host '  Then delete the temporary root:'
    Write-Host "    Remove-Item -Recurse -Force '$TempRoot'"
    Write-Host '========================================' -ForegroundColor Red
    exit 2
}

# ---------------------------------------------------------------------------
# Controlled cleanup — removes only the task WE created, then the temp root.
# On failure prints safe recovery commands instead of proceeding.
# ---------------------------------------------------------------------------
function Invoke-ControlledCleanup {
    Write-Host ''
    Write-Host '=== Cleanup ===' -ForegroundColor Cyan

    # 1. Remove the scheduled task ONLY if we created it.
    if ($Script:TaskCreatedByThisRun) {
        Write-Host '  Removing scheduled task (created by this run)...'
        try {
            $removeArgs = @('sync', 'schedule', 'remove', '--json')
            if ($Script:TaskInstalledWithElevation) { $removeArgs += '--elevated' }
            $removeResult = & $clientExe @removeArgs 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "sync schedule remove exited $LASTEXITCODE : $removeResult"
            }
            $removeObj = ($removeResult | Out-String).Trim() | ConvertFrom-Json
            if ($removeObj.status -notin @('removed', 'already_absent')) {
                throw "Unexpected removal status: $($removeObj.status)"
            }
        } catch {
            Exit-UnsafeCleanup -Reason "Task removal failed: $_"
        }

        # 2. Verify task absence via PowerShell directly.
        try {
            $verify = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction SilentlyContinue
            if ($verify) {
                throw "Task still registered after removal."
            }
        } catch {
            Exit-UnsafeCleanup -Reason "Task absence verification failed: $_"
        }
        Write-Host '  Task removed and verified absent.' -ForegroundColor Green
        Set-Gate 'TaskRemoved'
        Set-Gate 'TaskAbsenceVerified'
    } else {
        Write-Host '  Task was not created by this run — skipping removal.'
    }

    # 3. Verify Vault content was not touched by removal.
    Write-Host '  Checking vault integrity...'
    if (Test-Path $ObsidianDir) {
        Write-Host "  .obsidian: intact"
        Set-Gate 'VaultContentPreserved'
    } else {
        Write-Error '.obsidian directory was deleted — Vault content must never be touched!'
        Exit-UnsafeCleanup -Reason 'Vault content was deleted.'
    }

    # 4. Delete the temporary root (unless KeepArtifacts).
    if ($KeepArtifacts) {
        Write-Host ''
        Write-Host "  Keeping artifacts at: $TempRoot" -ForegroundColor Yellow
    } else {
        Write-Host "  Deleting temporary root: $TempRoot"
        Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
        if (Test-Path $TempRoot) {
            Write-Warning "Could not fully delete $TempRoot — some files may remain."
        }
    }

    # Clear env overrides.
    $env:MEDLEARN_SYNC_INSTALL_ROOT = ''
    $env:MEDLEARN_HOME = ''

    Set-Gate 'CleanupCompleted'
}

# ---------------------------------------------------------------------------
# Top-level try/catch/finally — ensures cleanup always runs.
# ---------------------------------------------------------------------------
try {
    # =======================================================================
    # STEP 1: Install
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 1: Dry-run install ===' -ForegroundColor Cyan
    $env:MEDLEARN_SYNC_INSTALL_ROOT = $InstallRoot
    $dryInstall = & medlearn sync install-windows --wheel $WheelPath --dry-run --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Dry-run install failed: $dryInstall" }
    $dryInstallObj = ($dryInstall | Out-String).Trim() | ConvertFrom-Json
    Write-Host "  status           : $($dryInstallObj.status)"
    Write-Host "  executable       : $($dryInstallObj.executable)"
    if ($dryInstallObj.executable -notmatch [regex]::Escape($InstallRoot)) {
        throw "Executable path not under install root."
    }

    Write-Host ''
    Write-Host '=== Step 2: Real install ===' -ForegroundColor Cyan
    $installResult = & medlearn sync install-windows --wheel $WheelPath --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Install failed: $installResult" }
    $installObj = ($installResult | Out-String).Trim() | ConvertFrom-Json
    Write-Host "  status: $($installObj.status)"
    Set-Gate 'Installed'

    # Verify stable executable.
    $clientExe = $installObj.executable
    if (-not (Test-Path $clientExe -PathType Leaf)) {
        throw "Installed executable not found: $clientExe"
    }
    $version = & $clientExe --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Installed executable --version failed: $version"
    }
    Write-Host "  client version: $version"
    Set-Gate 'ExecutableSmokeTest'

    # =======================================================================
    # STEP 3: Configure temporary Vault
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 3: Configure temporary Vault ===' -ForegroundColor Cyan
    New-Item -ItemType Directory -Force $ObsidianDir | Out-Null
    $env:MEDLEARN_HOME = $HomeDir
    $configResult = & $clientExe sync configure --endpoint $Endpoint --vault $VaultPath --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Configure failed: $configResult" }
    Write-Host "  $configResult"
    Set-Gate 'VaultConfigured'

    # =======================================================================
    # STEP 4: Login (interactive)
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 4: Login ===' -ForegroundColor Cyan
    Write-Host 'You will be prompted for the sync token. The token is NEVER written to'
    Write-Host 'disk, logs, task arguments, or PowerShell command history.'
    Write-Host ''
    & $clientExe sync login
    if ($LASTEXITCODE -ne 0) { throw 'Login failed.' }
    Set-Gate 'LoginCompleted'

    # =======================================================================
    # STEP 5: First dry-run
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 5: First dry-run ===' -ForegroundColor Cyan
    $dryRunResult = & $clientExe sync pull --dry-run --json 2>&1
    Write-Host "  $dryRunResult"
    if ($LASTEXITCODE -notin @(0, 3)) {
        throw "Dry-run failed with exit code $LASTEXITCODE."
    }
    Set-Gate 'DryRunCompleted'

    # =======================================================================
    # STEP 6: Explicit confirmation + first real pull
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 6: First real pull ===' -ForegroundColor Yellow
    $confirmation = Read-Host 'Ready for first real pull. Type YES to proceed'
    if ($confirmation -ne 'YES') {
        Write-Host 'Aborted by user.' -ForegroundColor Yellow
        exit 0
    }
    $pullResult = & $clientExe sync pull --confirm-first-pull --json 2>&1
    Write-Host "  $pullResult"
    if ($LASTEXITCODE -notin @(0, 3)) {
        throw "First pull failed with exit code $LASTEXITCODE."
    }
    Set-Gate 'FirstPullCompleted'

    # =======================================================================
    # STEP 7: Scheduled Task — what-if + install
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 7: Install Scheduled Task ===' -ForegroundColor Cyan

    # Re-confirm pre-flight (belt-and-suspenders).
    if (Test-AcceptanceTaskExists) {
        throw "Task '$($Script:TaskName)' appeared between pre-flight and install. Aborting."
    }

    # --what-if.
    $schedulePlanOutput = & $clientExe sync schedule install --what-if --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "schedule install --what-if failed: $schedulePlanOutput" }
    $schedulePlan = ($schedulePlanOutput | Out-String).Trim() | ConvertFrom-Json
    Write-Host "  what-if status : $($schedulePlan.status)"
    Write-Host "  task_name      : $($schedulePlan.task_name)"
    Write-Host "  interval       : $($schedulePlan.interval_minutes) minutes"
    if (Test-TokenExposure -Text ($schedulePlanOutput | Out-String) -Label '--what-if output') {
        throw 'Token exposure in --what-if output.'
    }

    # Real install.
    $scheduleResult = & $clientExe sync schedule install --interval-minutes 15 --json 2>&1
    if ($LASTEXITCODE -ne 0) {
        $scheduleError = ($scheduleResult | Out-String).Trim() | ConvertFrom-Json
        if ($scheduleError.error_code -ne 'SYNC_SCHEDULE_ELEVATION_REQUIRED') {
            throw "Schedule install failed: $scheduleResult"
        }
        Write-Host '  Task Scheduler requires one-time UAC approval for registration.' -ForegroundColor Yellow
        $approveElevation = Read-Host 'Type ELEVATE to approve registration only (or anything else to abort)'
        if ($approveElevation -ne 'ELEVATE') {
            throw 'Scheduled Task registration was not approved.'
        }
        $scheduleResult = & $clientExe sync schedule install --interval-minutes 15 --elevated --json 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Elevated schedule install failed: $scheduleResult" }
        $Script:TaskInstalledWithElevation = $true
    }
    $scheduleInstalledObj = ($scheduleResult | Out-String).Trim() | ConvertFrom-Json
    Write-Host "  schedule status: $($scheduleInstalledObj.status)"

    # Verify registration via PowerShell.
    if (-not (Test-AcceptanceTaskExists)) {
        throw "Task registration reported success but task not found via Get-ScheduledTask."
    }
    $registeredTask = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction Stop
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $currentAccount = $currentIdentity.Split('\')[-1]
    if ($registeredTask.Principal.UserId -notin @($currentIdentity, $currentAccount)) {
        throw "Task principal is not the current user: $($registeredTask.Principal.UserId)"
    }
    if ($registeredTask.Principal.LogonType.ToString() -ne 'Interactive') {
        throw "Task LogonType is not Interactive: $($registeredTask.Principal.LogonType)"
    }
    if ($registeredTask.Principal.RunLevel.ToString() -ne 'Limited') {
        throw "Task RunLevel is not Limited: $($registeredTask.Principal.RunLevel)"
    }
    Set-Gate 'TaskPrincipalVerified'
    $triggerTypes = @($registeredTask.Triggers | ForEach-Object { $_.CimClass.CimClassName })
    if ('MSFT_TaskLogonTrigger' -notin $triggerTypes -or 'MSFT_TaskTimeTrigger' -notin $triggerTypes) {
        throw "Task triggers are incomplete: $($triggerTypes -join ', ')"
    }
    $expectedWrapperPath = Join-Path $InstallRoot 'run-scheduled.ps1'
    $registeredAction = $registeredTask.Actions | Select-Object -First 1
    if ($registeredAction.Execute -notmatch '(?i)powershell\.exe$' -or
        $registeredAction.Arguments -notmatch [regex]::Escape($expectedWrapperPath)) {
        throw 'Task action does not invoke the expected scheduled wrapper.'
    }
    Set-Gate 'TaskDefinitionVerified'
    $Script:TaskCreatedByThisRun = $true
    Set-Gate 'TaskInstalled'

    # =======================================================================
    # STEP 8: Structured status
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 8: Structured task status ===' -ForegroundColor Cyan
    $statusResult = & $clientExe sync schedule status --json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Schedule status failed: $statusResult" }
    $statusObj = ($statusResult | Out-String).Trim() | ConvertFrom-Json
    Write-Host "  task_name        : $($statusObj.task_name)"
    Write-Host "  registered       : $($statusObj.registered)"
    Write-Host "  state            : $($statusObj.state)"
    Write-Host "  last_run_time    : $($statusObj.last_run_time)"
    Write-Host "  next_run_time    : $($statusObj.next_run_time)"
    Write-Host "  last_task_result : $($statusObj.last_task_result)"
    Write-Host "  executable       : $($statusObj.executable)"
    Write-Host "  principal        : $($statusObj.principal_user_id) / $($statusObj.principal_logon_type) / $($statusObj.principal_run_level)"
    Write-Host "  trigger_count    : $($statusObj.trigger_count)"
    if ($statusObj.registered -ne $true) {
        throw 'Task is not registered after install (structured status).'
    }
    if (Test-TokenExposure -Text ($statusResult | Out-String) -Label 'status output') {
        throw 'Token exposure in status output.'
    }
    Set-Gate 'TaskStatusValid'

    # =======================================================================
    # STEP 9: Token scan of on-disk artifacts (before task execution)
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 9: Token scan ===' -ForegroundColor Cyan
    $scanPaths = @(
        (Join-Path $HomeDir 'config.json'),
        (Join-Path $HomeDir 'rollout.json'),
        (Join-Path $HomeDir 'state.json'),
        (Join-Path $InstallRoot 'schedule.json'),
        (Join-Path $InstallRoot 'run-scheduled.ps1'),
        (Join-Path $InstallRoot 'install.json')
    )
    try {
        $taskObj = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction Stop
        if ($taskObj -and $taskObj.Actions.Count -gt 0) {
            if (Test-TokenExposure -Text $taskObj.Actions[0].Arguments -Label 'task action arguments') {
                throw 'Token exposure in task action arguments.'
            }
            Write-Host "  task action args  : clean"
        }
    } catch {
        if ($_.Exception.Message -match 'Token exposure') { throw }
        # Task not found here is unexpected at this point but not a token issue.
    }
    foreach ($path in $scanPaths) {
        if (Test-Path $path) {
            if (Test-FileTokenExposure -Path $path -Label (Split-Path $path -Leaf)) {
                throw "Token exposure in file: $path"
            }
            Write-Host "  $(Split-Path $path -Leaf) : clean"
        }
    }
    $credPath = Join-Path $HomeDir 'credential.bin'
    if (Test-Path $credPath) {
        $credBytes = [System.IO.File]::ReadAllBytes($credPath)
        $credText = [System.Text.Encoding]::UTF8.GetString($credBytes)
        if ($credText -match '^\s*[a-zA-Z0-9+/=]{32,}\s*$') {
            throw 'Credential file appears to be plaintext, not DPAPI ciphertext.'
        }
        Write-Host '  credential.bin    : DPAPI ciphertext (not plaintext)'
    }
    Set-Gate 'TokenScanPassed'

    # =======================================================================
    # STEP 10: Start task and STRICTLY verify execution
    # =======================================================================
    Write-Host ''
    Write-Host '=== Step 10: Start task and verify execution ===' -ForegroundColor Cyan

    # --- Capture baselines ---
    $TestStartTimestamp = Get-Date
    $BaselineState = $null
    $BaselineLastRunTime = [datetime]::MinValue
    $BaselineLogCount = 0
    $logPath = Join-Path $HomeDir 'scheduled-results.jsonl'

    try {
        $preInfo = Get-ScheduledTaskInfo -TaskName $Script:TaskName -ErrorAction Stop
        if ($preInfo -and $preInfo.LastRunTime.Year -gt 2000) {
            $BaselineLastRunTime = $preInfo.LastRunTime
        }
        $preTask = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction Stop
        if ($preTask) { $BaselineState = $preTask.State.ToString() }
    } catch {
        throw "Cannot read baseline task info: $_"
    }
    if (Test-Path $logPath) {
        try {
            $existingLines = @(Get-Content $logPath -ErrorAction Stop | Where-Object { $_.Trim() -ne '' })
            $BaselineLogCount = $existingLines.Count
        } catch {
            Write-Warning "Could not read baseline log: $_"
        }
    }
    Write-Host "  Baseline LastRunTime : $BaselineLastRunTime"
    Write-Host "  Baseline log records  : $BaselineLogCount"
    Write-Host "  Test start            : $($TestStartTimestamp.ToString('o'))"

    # --- Start task ---
    try {
        Start-ScheduledTask -TaskName $Script:TaskName -ErrorAction Stop
    } catch {
        throw "Start-ScheduledTask threw: $_"
    }
    Write-Host '  Task start requested.'
    Set-Gate 'TaskStarted'

    # --- Wait with bounded timeout ---
    $deadline = (Get-Date).AddMinutes($Script:TaskTimeoutMinutes)
    $TaskObserved = $false
    $TaskLastRunTimeAdvanced = $false
    $TaskStateNotRunning = $false
    $TaskLastResultZero = $false

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        try {
            $info = Get-ScheduledTaskInfo -TaskName $Script:TaskName -ErrorAction Stop
        } catch {
            continue
        }
        if ($null -eq $info) { continue }

        $TaskObserved = $true

        if ($info.LastRunTime -gt $BaselineLastRunTime -and
            $info.LastRunTime -gt $TestStartTimestamp) {
            $TaskLastRunTimeAdvanced = $true
            Write-Host "  LastRunTime    : $($info.LastRunTime.ToString('o'))"
            Write-Host "  LastTaskResult : $($info.LastTaskResult)"

            $currentTask = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction SilentlyContinue
            $currentState = if ($currentTask) { $currentTask.State.ToString() } else { 'Unknown' }
            Write-Host "  Current state  : $currentState"

            if ($currentState -ne 'Running') {
                $TaskStateNotRunning = $true
                if ($info.LastTaskResult -eq 0) {
                    $TaskLastResultZero = $true
                    Write-Host '  Task completed successfully (LastTaskResult=0).' -ForegroundColor Green
                } else {
                    Write-Host ("  Task exited with non-zero result: 0x{0:X8}" -f $info.LastTaskResult) -ForegroundColor Red
                }
                break
            }
        }
    }

    # --- Validate each execution criterion ---
    if (-not $TaskObserved) {
        throw 'Task was never observed via Get-ScheduledTaskInfo within timeout.'
    }

    if (-not $TaskLastRunTimeAdvanced) {
        $nowInfo = Get-ScheduledTaskInfo -TaskName $Script:TaskName -ErrorAction SilentlyContinue
        $lr = if ($nowInfo) { $nowInfo.LastRunTime } else { 'N/A' }
        throw "LastRunTime did not advance.  Baseline=$BaselineLastRunTime  Current=$lr"
    }
    Set-Gate 'TaskObserved'
    Set-Gate 'TaskLastRunTimeAdvanced'

    if (-not $TaskStateNotRunning) {
        throw 'Task is still Running after timeout.'
    }
    Set-Gate 'TaskStateNotRunning'

    # Non-zero LastTaskResult is FATAL.
    if (-not $TaskLastResultZero) {
        $finalInfo = Get-ScheduledTaskInfo -TaskName $Script:TaskName -ErrorAction SilentlyContinue
        $hr = if ($finalInfo) { "0x{0:X8}" -f $finalInfo.LastTaskResult } else { 'N/A' }
        throw "Task completed with non-zero LastTaskResult: $hr"
    }
    Set-Gate 'TaskLastResultZero'

    # --- Verify scheduled log ---
    if (-not (Test-Path $logPath)) {
        throw "Scheduled log file not found: $logPath"
    }
    Set-Gate 'ScheduledLogExists'

    $logLines = @(Get-Content $logPath -ErrorAction Stop | Where-Object { $_.Trim() -ne '' })
    $newLogCount = $logLines.Count
    if ($newLogCount -le $BaselineLogCount) {
        throw "No new scheduled log records.  Baseline=$BaselineLogCount  Current=$newLogCount"
    }
    Set-Gate 'ScheduledLogNewRecord'
    Write-Host "  Scheduled log records: $newLogCount (was $BaselineLogCount)"

    # Validate the newest record.
    $lastRecord = $null
    try {
        $lastRecord = $logLines[-1] | ConvertFrom-Json
    } catch {
        throw "Last scheduled log line is not valid JSON: $($logLines[-1])"
    }
    if ($lastRecord.status -ne 'synced') {
        throw "Last scheduled log record status is '$($lastRecord.status)', expected 'synced'."
    }
    if (Get-Member -InputObject $lastRecord -Name 'error_code' -MemberType Properties) {
        throw "Last scheduled log record contains error_code=$($lastRecord.error_code)."
    }
    if ($lastRecord.client_version -ne $version.Trim()) {
        Write-Warning "Client version mismatch: log=$($lastRecord.client_version) exe=$version"
    }
    foreach ($field in @('remote_count', 'downloaded_count', 'unchanged_count', 'conflict_count')) {
        if ($null -eq $lastRecord.$field) {
            throw "Last scheduled log record missing field: $field"
        }
    }
    Set-Gate 'ScheduledLogRecordValid'
    Write-Host "  Last record status : $($lastRecord.status)"
    Write-Host "  Client version     : $($lastRecord.client_version)"

    # --- Verify wrapper references correct MEDLEARN_HOME ---
    $wrapperPath = Join-Path $InstallRoot 'run-scheduled.ps1'
    if (-not (Test-Path $wrapperPath)) {
        throw "Wrapper script not found: $wrapperPath"
    }
    $wrapperContent = Get-Content -Raw $wrapperPath -ErrorAction Stop
    if ($wrapperContent -notmatch [regex]::Escape($HomeDir)) {
        throw "Wrapper does not reference the expected MEDLEARN_HOME: $HomeDir"
    }
    Set-Gate 'WrapperHomeMatches'
    Write-Host "  Wrapper MEDLEARN_HOME: correct"

    # --- Post-execution token scan ---
    if (Test-Path $logPath) {
        if (Test-FileTokenExposure -Path $logPath -Label 'scheduled-results.jsonl') {
            throw 'Token exposure in scheduled log.'
        }
    }

    # =======================================================================
    # STEP 11: Remove task (via Invoke-ControlledCleanup)
    # =======================================================================
    # Cleanup is handled in the finally block; we call it explicitly here
    # so that the final report reflects the outcome before exit.
    Invoke-ControlledCleanup

    # =======================================================================
    # FINAL REPORT — only reached if ALL gates passed
    # =======================================================================
    Write-Host ''
    Write-Host '========================================' -ForegroundColor Green
    Write-Host '  ACCEPTANCE TEST PASSED' -ForegroundColor Green
    Write-Host '========================================' -ForegroundColor Green
    Write-Host "  Installed               : $clientExe"
    Write-Host "  Version                 : $version"
    Write-Host "  Vault                   : $VaultPath"
    Write-Host "  Task installed          : yes (created by this run)"
    Write-Host "  Task started            : yes"
    Write-Host "  Task completed          : yes (LastTaskResult = 0)"
    Write-Host "  New scheduled log       : verified"
    Write-Host "  Task removed            : yes (verified absent)"
    Write-Host "  Token in files          : none"
    Write-Host "  Vault preserved         : yes"
    Write-Host '========================================' -ForegroundColor Green
    exit 0

} catch {
    # -------------------------------------------------------------------
    # Any exception during the acceptance workflow is fatal.
    # -------------------------------------------------------------------
    Write-Host ''
    Write-Host '========================================' -ForegroundColor Red
    Write-Host '  ACCEPTANCE TEST FAILED' -ForegroundColor Red
    Write-Host '========================================' -ForegroundColor Red
    Write-Host "  Error: $_"
    Write-Host ''
    Write-Host '  Gates passed:'
    foreach ($key in $Gate.Keys | Sort-Object) {
        $mark = if ($Gate[$key]) { '[x]' } else { '[ ]' }
        Write-Host "    $mark $key"
    }
    Write-Host ''
    Write-Host '  Diagnostic artifacts:'
    Write-Host "    Temp root: $TempRoot"
    if ($Script:TaskCreatedByThisRun) {
        Write-Host "    Task may still be registered; remove manually:"
        Write-Host "      Unregister-ScheduledTask -TaskName '$($Script:TaskName)' -Confirm:`$false"
    }
    Write-Host '========================================' -ForegroundColor Red
    exit 2

} finally {
    # -------------------------------------------------------------------
    # Always attempt controlled cleanup.  If we already cleaned up
    # successfully above, this is a no-op.  If we crashed mid-flight,
    # try to clean up what we can, but never swallow the error.
    # -------------------------------------------------------------------
    if ($Gate['CleanupCompleted']) {
        # Already cleaned up successfully.
    } elseif ($Script:TaskCreatedByThisRun) {
        # We created the task and haven't cleaned up yet — attempt emergency
        # removal but don't suppress the original error.
        Write-Host ''
        Write-Host '=== Emergency cleanup: removing task created by this run ===' -ForegroundColor Yellow
        try {
            $emergencyArgs = @('sync', 'schedule', 'remove', '--json')
            if ($Script:TaskInstalledWithElevation) { $emergencyArgs += '--elevated' }
            $emergency = & $clientExe @emergencyArgs 2>&1
            if ($LASTEXITCODE -eq 0) {
                $verifyEm = Get-ScheduledTask -TaskName $Script:TaskName -ErrorAction SilentlyContinue
                if ($verifyEm) {
                    Write-Error 'Emergency removal: task still present after remove command.'
                } else {
                    Write-Host '  Emergency removal succeeded.' -ForegroundColor Green
                }
            } else {
                Write-Error "Emergency removal failed (exit $LASTEXITCODE): $emergency"
            }
        } catch {
            Write-Error "Emergency removal threw: $_"
        }
        # Do NOT delete the temp root on emergency path — retain for diagnosis.
        Write-Host "  Temporary root retained for diagnosis: $TempRoot" -ForegroundColor Yellow
    }
}
