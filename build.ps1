$ErrorActionPreference = "Stop"

poetry run pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name singbox-outbound-updater `
    --copy-metadata singbox-outbound-updater `
    --hidden-import keyring.backends.Windows `
    main.py

Write-Host "Portable application: dist\singbox-outbound-updater.exe"
