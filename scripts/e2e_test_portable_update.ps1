param(
    [Parameter(Mandatory=$true)][string]$ExecutablePath,
    [ValidateRange(30, 300)][int]$TimeoutSeconds = 150
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$systemWhere = Join-Path $env:SystemRoot "System32\where.exe"
$testRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("SingRoute-update-e2e-" + [Guid]::NewGuid().ToString("N"))
$harnessRoot = Join-Path $testRoot "harness"
$harnessDist = Join-Path $harnessRoot "dist"
$harnessBuild = Join-Path $harnessRoot "build"
$harnessSpec = Join-Path $harnessRoot "spec"
$harnessExecutable = Join-Path $harnessDist "SingRoute.exe"
$updaterResource = Join-Path $projectRoot "singroute\application\update.ps1"
$scenarioTargets = [System.Collections.Generic.List[string]]::new()
$startedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$previousQtPlatform = $env:QT_QPA_PLATFORM
$previousResetEnvironment = $env:PYINSTALLER_RESET_ENVIRONMENT
$failureMessage = $null
$cleanupMessages = [System.Collections.Generic.List[string]]::new()

$nativeMethods = Add-Type -Name "SingRouteUpdateE2ENativeMethods" `
    -Namespace "SingRoute.UpdateE2E" `
    -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
[return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
public static extern bool SetDllDirectory(string lpPathName);
"@ `
    -PassThru
if (-not $nativeMethods::SetDllDirectory($null)) {
    throw "Windows could not reset the E2E test DLL search path."
}

function Get-Sha256 {
    param([Parameter(Mandatory=$true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-ProcessesAtPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $matches = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
    foreach ($process in [System.Diagnostics.Process]::GetProcessesByName("SingRoute")) {
        try {
            if (
                [System.IO.Path]::GetFullPath($process.MainModule.FileName) -ieq
                $resolvedPath
            ) {
                $matches.Add($process)
            }
            else {
                $process.Dispose()
            }
        }
        catch {
            $process.Dispose()
        }
    }
    return $matches.ToArray()
}

function Stop-ProcessTree {
    param([Parameter(Mandatory=$true)][System.Diagnostics.Process]$Process)
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $output = & "$env:SystemRoot\System32\taskkill.exe" `
                /PID $Process.Id /T /F 2>&1
            if ($LASTEXITCODE -ne 0) {
                $Process.Refresh()
                if (-not $Process.HasExited) {
                    throw "taskkill failed for PID $($Process.Id): $($output -join ' ')"
                }
            }
            if (-not $Process.HasExited) {
                $Process.WaitForExit(10000) | Out-Null
            }
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Stop-ProcessesAtPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    foreach ($process in Get-ProcessesAtPath -Path $Path) {
        Stop-ProcessTree -Process $process
    }
}

function Wait-ForCondition {
    param(
        [Parameter(Mandatory=$true)][scriptblock]$Condition,
        [Parameter(Mandatory=$true)][string]$Failure,
        [Parameter(Mandatory=$true)][int]$Seconds
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    $lastConditionError = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if (& $Condition) {
                return
            }
            $lastConditionError = $null
        }
        catch {
            # Atomic replacement deliberately leaves a short interval without
            # the target path. Treat transient observation errors like a false
            # condition and keep waiting for the complete stable state.
            $lastConditionError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -ne $lastConditionError) {
        throw "$Failure Last observation error: $lastConditionError"
    }
    throw $Failure
}

function Write-UpdatePlan {
    param(
        [Parameter(Mandatory=$true)][string]$Directory,
        [Parameter(Mandatory=$true)][string]$PayloadPath
    )
    $planPath = Join-Path $Directory ".SingRoute.e2e-plan.json"
    $plan = @{
        payload_path = [System.IO.Path]::GetFullPath($PayloadPath)
        version = "9.9.9"
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText(
        $planPath,
        $plan,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Start-UpdateScenario {
    param([Parameter(Mandatory=$true)][string]$TargetPath)
    $directory = Split-Path -Parent $TargetPath
    $stdoutPath = Join-Path $directory "harness-stdout.log"
    $stderrPath = Join-Path $directory "harness-stderr.log"
    $process = Start-Process `
        -FilePath $TargetPath `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -ErrorAction Stop
    $startedProcesses.Add($process)
    return $process
}

try {
    New-Item -ItemType Directory -Path $harnessDist | Out-Null
    New-Item -ItemType Directory -Path $harnessBuild | Out-Null
    New-Item -ItemType Directory -Path $harnessSpec | Out-Null
    Push-Location $projectRoot
    try {
        $resourceCheckCode = @'
import sys
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
from scripts.check_executable_icon import check_executable_icon

check_executable_icon(Path(sys.argv[1]), Path("singroute/assets/singroute.ico"))
resource_name = r"singroute\application\update.ps1"
embedded = CArchiveReader(sys.argv[1]).extract(resource_name)
expected = Path(sys.argv[2]).read_bytes()
if embedded != expected:
    raise SystemExit("production EXE contains a missing or stale update.ps1")
'@
        $resourceCheckOutput = poetry run python `
            -c $resourceCheckCode `
            $resolvedExecutable `
            $updaterResource 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw (
                "Production updater resource check failed: " +
                ($resourceCheckOutput -join [Environment]::NewLine)
            )
        }

        poetry run pyinstaller `
            --noconfirm `
            --clean `
            --onefile `
            --console `
            --name SingRoute `
            --distpath $harnessDist `
            --workpath $harnessBuild `
            --specpath $harnessSpec `
            --paths $projectRoot `
            --add-data "${updaterResource};singroute/application" `
            scripts\portable_update_harness.py
        if ($LASTEXITCODE -ne 0) {
            throw "Updater E2E harness build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $harnessExecutable -PathType Leaf)) {
        throw "PyInstaller did not create the updater E2E harness."
    }

    $env:QT_QPA_PLATFORM = "offscreen"
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    $productionDigest = Get-Sha256 -Path $resolvedExecutable
    $harnessDigest = Get-Sha256 -Path $harnessExecutable

    # Successful replacement: a frozen old process downloads, installs, and
    # starts the actual production GUI executable.
    $successDirectory = Join-Path $testRoot "success"
    New-Item -ItemType Directory -Path $successDirectory | Out-Null
    $successTarget = Join-Path $successDirectory "SingRoute.exe"
    $successPayload = Join-Path $successDirectory "candidate-source.exe"
    Copy-Item -LiteralPath $harnessExecutable -Destination $successTarget
    Copy-Item -LiteralPath $resolvedExecutable -Destination $successPayload
    Write-UpdatePlan -Directory $successDirectory -PayloadPath $successPayload
    $scenarioTargets.Add($successTarget)
    $successInitial = Start-UpdateScenario -TargetPath $successTarget
    $successStaged = Join-Path $successDirectory ".SingRoute.update-v9.9.9.exe"
    $successBackup = Join-Path $successDirectory ".SingRoute.previous.exe"
    $successReady = Join-Path $successDirectory ".SingRoute.update-ready"
    $successHealth = Join-Path $successDirectory ".SingRoute.update-health"
    $successError = Join-Path $successDirectory "SingRoute-update-error.txt"
    $successDownload = Join-Path $successDirectory ".SingRoute.e2e-downloaded"
    $successHelper = Join-Path $successDirectory ".SingRoute.e2e-helper-ready"
    Wait-ForCondition -Seconds $TimeoutSeconds -Failure (
        "The packaged update did not finish successfully."
    ) -Condition {
        $running = @(Get-ProcessesAtPath -Path $successTarget)
        foreach ($process in $running) { $process.Dispose() }
        return (
            $running.Count -ge 1 `
            -and (Test-Path -LiteralPath $successHelper) `
            -and (Test-Path -LiteralPath $successDownload) `
            -and (Get-Sha256 -Path $successTarget) -ceq $productionDigest `
            -and -not (Test-Path -LiteralPath $successStaged) `
            -and -not (Test-Path -LiteralPath $successBackup) `
            -and -not (Test-Path -LiteralPath $successReady) `
            -and -not (Test-Path -LiteralPath $successHealth) `
            -and -not (Test-Path -LiteralPath $successError)
        )
    }
    $successInitial.Refresh()
    if (-not $successInitial.HasExited) {
        throw "The previous packaged process did not exit after a successful update."
    }
    if (
        [System.IO.File]::ReadAllText($successDownload).Trim() -cne
        $productionDigest
    ) {
        throw "The successful scenario downloaded an unexpected payload."
    }
    Stop-ProcessesAtPath -Path $successTarget

    # Failed replacement: the candidate exits without the health token, so the
    # helper must restore and launch the frozen previous version.
    $rollbackDirectory = Join-Path $testRoot "rollback"
    New-Item -ItemType Directory -Path $rollbackDirectory | Out-Null
    $rollbackTarget = Join-Path $rollbackDirectory "SingRoute.exe"
    $rollbackPayload = Join-Path $rollbackDirectory "candidate-source.exe"
    Copy-Item -LiteralPath $harnessExecutable -Destination $rollbackTarget
    Copy-Item -LiteralPath $systemWhere -Destination $rollbackPayload
    Write-UpdatePlan -Directory $rollbackDirectory -PayloadPath $rollbackPayload
    $scenarioTargets.Add($rollbackTarget)
    $rollbackInitial = Start-UpdateScenario -TargetPath $rollbackTarget
    $rollbackStaged = Join-Path $rollbackDirectory ".SingRoute.update-v9.9.9.exe"
    $rollbackBackup = Join-Path $rollbackDirectory ".SingRoute.previous.exe"
    $rollbackReady = Join-Path $rollbackDirectory ".SingRoute.update-ready"
    $rollbackHealth = Join-Path $rollbackDirectory ".SingRoute.update-health"
    $rollbackMarker = Join-Path $rollbackDirectory ".SingRoute.e2e-rollback-restarted"
    $failedDigest = Get-Sha256 -Path $rollbackPayload
    Wait-ForCondition -Seconds $TimeoutSeconds -Failure (
        "The packaged update did not restore and restart the previous version."
    ) -Condition {
        $running = @(Get-ProcessesAtPath -Path $rollbackTarget)
        foreach ($process in $running) { $process.Dispose() }
        return (
            $running.Count -ge 1 `
            -and (Test-Path -LiteralPath $rollbackMarker) `
            -and (Test-Path -LiteralPath $rollbackStaged) `
            -and (Get-Sha256 -Path $rollbackTarget) -ceq $harnessDigest `
            -and (Get-Sha256 -Path $rollbackStaged) -ceq $failedDigest `
            -and -not (Test-Path -LiteralPath $rollbackBackup) `
            -and -not (Test-Path -LiteralPath $rollbackReady) `
            -and -not (Test-Path -LiteralPath $rollbackHealth)
        )
    }
    $rollbackInitial.Refresh()
    if (-not $rollbackInitial.HasExited) {
        throw "The original packaged process did not exit before rollback."
    }
    $rollbackMessage = [System.IO.File]::ReadAllText($rollbackMarker)
    if ($rollbackMessage -notmatch "exited before reporting readiness") {
        throw "Rollback did not preserve the failed-start diagnostic."
    }
    Stop-ProcessesAtPath -Path $rollbackTarget
}
catch {
    $failureMessage = $_.Exception.Message
}
finally {
    $env:QT_QPA_PLATFORM = $previousQtPlatform
    $env:PYINSTALLER_RESET_ENVIRONMENT = $previousResetEnvironment
    foreach ($process in $startedProcesses) {
        try {
            Stop-ProcessTree -Process $process
        }
        catch {
            $cleanupMessages.Add($_.Exception.Message)
        }
    }
    foreach ($target in $scenarioTargets) {
        try {
            Stop-ProcessesAtPath -Path $target
        }
        catch {
            $cleanupMessages.Add($_.Exception.Message)
        }
    }
    $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(15)
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    $expectedParent = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    if ([System.IO.Path]::GetDirectoryName($resolvedTestRoot) -ine $expectedParent) {
        throw "Unexpected E2E cleanup directory: $resolvedTestRoot"
    }
    while (Test-Path -LiteralPath $testRoot) {
        try {
            Remove-Item -Recurse -Force -LiteralPath $testRoot
        }
        catch {
            if ([DateTime]::UtcNow -ge $cleanupDeadline) {
                $cleanupMessages.Add(
                    "Could not remove E2E directory $testRoot`: $($_.Exception.Message)"
                )
                break
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

if ($cleanupMessages.Count -gt 0) {
    $cleanupFailure = $cleanupMessages -join "; "
    if ($null -eq $failureMessage) {
        $failureMessage = $cleanupFailure
    }
    else {
        $failureMessage = "$failureMessage Cleanup failure: $cleanupFailure"
    }
}
if ($null -ne $failureMessage) {
    throw $failureMessage
}
Write-Host "Portable self-update and rollback E2E test passed."
