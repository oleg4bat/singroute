$ErrorActionPreference = "Stop"

poetry run pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name SingRoute `
    --copy-metadata singroute `
    --hidden-import keyring.backends.Windows `
    main.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$outputPath = Join-Path $PSScriptRoot "dist\SingRoute.exe"
if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    throw "PyInstaller did not create $outputPath"
}

Write-Host "Portable application: dist\SingRoute.exe"
