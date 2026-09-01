param(
    [Parameter(Mandatory=$true)][int]$SingRouteProcessId,
    [int]$SingRouteParentProcessId = 0,
    [Parameter(Mandatory=$true)][string]$StagedPath,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][string]$BackupPath,
    [Parameter(Mandatory=$true)][string]$ErrorPath,
    [Parameter(Mandatory=$true)][string]$ReadyPath,
    [Parameter(Mandatory=$true)][string]$HealthPath,
    [Parameter(Mandatory=$true)][string]$HealthToken,
    [Parameter(Mandatory=$true)][string]$ScriptPath,
    [ValidateRange(1, 600)][int]$ExitTimeoutSeconds = 120,
    [ValidateRange(1, 300)][int]$HealthTimeoutSeconds = 45,
    [ValidateRange(1, 30)][int]$StartupStabilitySeconds = 3,
    [ValidateRange(1, 120)][int]$RollbackTimeoutSeconds = 30,
    [ValidateRange(1, 30)][int]$RestartStabilitySeconds = 2
)
$ErrorActionPreference = "Stop"

function Test-TargetProcessRunning {
    param(
        [Parameter(Mandatory=$true)][string]$ExecutablePath,
        [int[]]$ExcludedProcessIds = @()
    )
    $resolvedTarget = [System.IO.Path]::GetFullPath($ExecutablePath)
    $processName = [System.IO.Path]::GetFileNameWithoutExtension($resolvedTarget)
    foreach ($process in [System.Diagnostics.Process]::GetProcessesByName($processName)) {
        try {
            if ($ExcludedProcessIds -contains $process.Id) { continue }
            $candidatePath = $process.MainModule.FileName
            if (
                [string]::Equals(
                    [System.IO.Path]::GetFullPath($candidatePath),
                    $resolvedTarget,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                return $true
            }
        }
        catch {
            # If a same-name process is elevated, its path may be inaccessible.
            # Treat it as a conflict rather than risk replacing a live target.
            return $true
        }
        finally {
            $process.Dispose()
        }
    }
    return $false
}

function Move-WithRetry {
    param(
        [Parameter(Mandatory=$true)][string]$Source,
        [Parameter(Mandatory=$true)][string]$Destination,
        [Parameter(Mandatory=$true)][DateTime]$Deadline
    )
    while ($true) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
            return
        }
        catch {
            if ([DateTime]::UtcNow -ge $Deadline) { throw }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Write-FailureFile {
    param([Parameter(Mandatory=$true)][string]$Message)
    try {
        [System.IO.File]::WriteAllText(
            $ErrorPath,
            $Message,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    catch {}
}

$originalProcessExited = $false
$backupCreated = $false
$newVersionInstalled = $false
$newProcess = $null
try {
    if (
        Test-TargetProcessRunning `
            -ExecutablePath $TargetPath `
            -ExcludedProcessIds @(
                $SingRouteProcessId,
                $SingRouteParentProcessId
            )
    ) {
        throw "Another SingRoute process is already using $TargetPath."
    }
    [System.IO.File]::WriteAllText($ReadyPath, "ready")

    $deadline = [DateTime]::UtcNow.AddSeconds($ExitTimeoutSeconds)
    while (Get-Process -Id $SingRouteProcessId -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "SingRoute did not exit before the update deadline."
        }
        Start-Sleep -Milliseconds 250
    }
    $originalProcessExited = $true

    if (
        Test-TargetProcessRunning `
            -ExecutablePath $TargetPath `
            -ExcludedProcessIds @($SingRouteParentProcessId)
    ) {
        throw "Another SingRoute process started before the update could be installed."
    }

    if (Test-Path -LiteralPath $BackupPath) {
        throw "A previous update backup already exists: $BackupPath"
    }
    Move-WithRetry -Source $TargetPath -Destination $BackupPath -Deadline $deadline
    $backupCreated = $true

    Move-Item -LiteralPath $StagedPath -Destination $TargetPath -ErrorAction Stop
    $newVersionInstalled = $true

    Remove-Item -Force -LiteralPath $HealthPath -ErrorAction SilentlyContinue
    $env:SINGROUTE_UPDATE_HEALTH_PATH = $HealthPath
    $env:SINGROUTE_UPDATE_HEALTH_TOKEN = $HealthToken
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    $newProcess = Start-Process -FilePath $TargetPath -PassThru -ErrorAction Stop

    $healthDeadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $healthDeadline) {
        $newProcess.Refresh()
        if ($newProcess.HasExited) {
            throw "Updated SingRoute exited before reporting readiness (code $($newProcess.ExitCode))."
        }
        if (Test-Path -LiteralPath $HealthPath) {
            try {
                $reportedToken = [System.IO.File]::ReadAllText($HealthPath).Trim()
                if ($reportedToken -ceq $HealthToken) {
                    $healthy = $true
                    break
                }
            }
            catch {}
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $healthy) {
        throw "Updated SingRoute did not report readiness before the deadline."
    }

    $stabilityDeadline = [DateTime]::UtcNow.AddSeconds($StartupStabilitySeconds)
    while ([DateTime]::UtcNow -lt $stabilityDeadline) {
        $newProcess.Refresh()
        if ($newProcess.HasExited) {
            throw "Updated SingRoute exited during the startup check (code $($newProcess.ExitCode))."
        }
        Start-Sleep -Milliseconds 100
    }

    Remove-Item Env:SINGROUTE_UPDATE_HEALTH_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:SINGROUTE_UPDATE_HEALTH_TOKEN -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $HealthPath -ErrorAction SilentlyContinue
    try {
        Remove-Item -Force -LiteralPath $BackupPath -ErrorAction Stop
        $backupCreated = $false
    }
    catch {
        # The healthy update remains installed; a retained backup safely blocks
        # another update until the user inspects or removes it.
    }
    Remove-Item -Force -LiteralPath $ErrorPath -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $failure = $_.Exception.ToString()
    Remove-Item Env:SINGROUTE_UPDATE_HEALTH_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:SINGROUTE_UPDATE_HEALTH_TOKEN -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $HealthPath -ErrorAction SilentlyContinue

    if ($null -ne $newProcess) {
        try {
            $newProcess.Refresh()
            if (-not $newProcess.HasExited) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID $newProcess.Id /T /F | Out-Null
                $newProcess.WaitForExit(10000) | Out-Null
            }
        }
        catch {}
    }

    $previousVersionReady = $false
    $previousVersionAlreadyRunning = $false
    $rollbackFailure = $null
    try {
        if ($backupCreated) {
            if (-not (Test-Path -LiteralPath $BackupPath)) {
                throw "The previous-version backup disappeared during rollback."
            }
            $rollbackDeadline = [DateTime]::UtcNow.AddSeconds($RollbackTimeoutSeconds)
            if ($newVersionInstalled) {
                if (Test-Path -LiteralPath $TargetPath) {
                    if (Test-Path -LiteralPath $StagedPath) {
                        throw "The staged update path is unexpectedly occupied during rollback."
                    }
                    Move-WithRetry `
                        -Source $TargetPath `
                        -Destination $StagedPath `
                        -Deadline $rollbackDeadline
                }
                else {
                    $failure = (
                        "$failure`r`n`r`nThe failed updated executable " +
                        "disappeared before rollback; restoring the backup."
                    )
                }
                $newVersionInstalled = $false
            }
            elseif (Test-Path -LiteralPath $TargetPath) {
                throw "The target path is unexpectedly occupied during rollback."
            }
            Move-WithRetry `
                -Source $BackupPath `
                -Destination $TargetPath `
                -Deadline $rollbackDeadline
            $backupCreated = $false
            $previousVersionReady = $true
        }
        elseif (
            $originalProcessExited `
            -and -not $newVersionInstalled `
            -and (Test-Path -LiteralPath $TargetPath)
        ) {
            # This attempt did not move the original executable at all.
            $previousVersionReady = $true
            $previousVersionAlreadyRunning = Test-TargetProcessRunning `
                -ExecutablePath $TargetPath
        }
        elseif (-not $originalProcessExited) {
            $originalProcess = Get-Process `
                -Id $SingRouteProcessId `
                -ErrorAction SilentlyContinue
            if ($null -ne $originalProcess) {
                $previousVersionReady = $true
                $previousVersionAlreadyRunning = $true
                $originalProcess.Dispose()
            }
            elseif (
                (Test-Path -LiteralPath $TargetPath) `
                -and (Test-TargetProcessRunning -ExecutablePath $TargetPath)
            ) {
                $previousVersionReady = $true
                $previousVersionAlreadyRunning = $true
            }
        }
    }
    catch {
        $rollbackFailure = $_.Exception.ToString()
    }

    if ($null -ne $rollbackFailure) {
        $failure = "$failure`r`n`r`nRollback failure:`r`n$rollbackFailure"
    }
    if (-not $previousVersionReady) {
        $failure = (
            "$failure`r`n`r`nThe previous version was not restarted because " +
            "a safe rollback could not be confirmed."
        )
        Write-FailureFile -Message $failure
        exit 1
    }

    Write-FailureFile -Message $failure
    if ($previousVersionAlreadyRunning) {
        exit 1
    }
    try {
        $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
        $restoredProcess = Start-Process `
            -FilePath $TargetPath `
            -PassThru `
            -ErrorAction Stop
        $restartDeadline = [DateTime]::UtcNow.AddSeconds($RestartStabilitySeconds)
        while ([DateTime]::UtcNow -lt $restartDeadline) {
            $restoredProcess.Refresh()
            if ($restoredProcess.HasExited) {
                throw "Restored SingRoute exited immediately (code $($restoredProcess.ExitCode))."
            }
            Start-Sleep -Milliseconds 100
        }
    }
    catch {
        $failure = "$failure`r`n`r`nPrevious-version restart failure:`r`n$($_.Exception)"
        Write-FailureFile -Message $failure
    }
    exit 1
}
finally {
    Remove-Item -Force -LiteralPath $ReadyPath -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $HealthPath -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $ScriptPath -ErrorAction SilentlyContinue
}
