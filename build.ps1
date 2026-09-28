$ErrorActionPreference = "Stop"

# Local builds use the EXE alone. A legacy sidecar may describe an older build.
$legacyChecksumPath = Join-Path $PSScriptRoot "dist\SingRoute.exe.sha256"
if (Test-Path -LiteralPath $legacyChecksumPath -PathType Leaf) {
    Remove-Item -LiteralPath $legacyChecksumPath -Force
}

poetry run pyinstaller `
    --noconfirm `
    --clean `
    --distpath dist `
    --workpath build `
    installer\SingRoute.spec

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$outputPath = Join-Path $PSScriptRoot "dist\SingRoute.exe"
if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    throw "PyInstaller did not create $outputPath"
}

Write-Host "Portable application: dist\SingRoute.exe"
