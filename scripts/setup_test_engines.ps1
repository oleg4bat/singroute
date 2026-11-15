$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) '.pytest-engines'
New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null

# Pin the same engine versions for local integration tests, CI and releases.
$engines = @(
    @{
        Variable = 'SING_BOX_BINARY'
        Url = 'https://github.com/SagerNet/sing-box/releases/download/v1.14.2/sing-box-1.14.2-windows-amd64.zip'
        Sha256 = 'c2d8bfff918755808781dfdeeb8581b6c91eb3a243d9a7b55483cfc0c0684d32'
        Executable = 'sing-box-1.14.2-windows-amd64/sing-box.exe'
    },
    @{
        Variable = 'XRAY_BINARY'
        Url = 'https://github.com/XTLS/Xray-core/releases/download/v26.10.21/Xray-windows-64.zip'
        Sha256 = 'fc58c39ee2f2717b4c54d28d374362389e5319b20ae44aea76ea45a5243ec827'
        Executable = 'xray.exe'
    }
)

foreach ($engine in $engines) {
    $archivePath = Join-Path $runtimeDirectory ($engine.Variable + '.zip')
    $validArchive = (Test-Path -LiteralPath $archivePath -PathType Leaf) -and
        ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ieq $engine.Sha256)
    if (-not $validArchive) {
        $partialPath = $archivePath + '.part'
        try {
            Invoke-WebRequest -Uri $engine.Url -OutFile $partialPath -UseBasicParsing
            if ((Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash -ine $engine.Sha256) {
                throw "Unexpected archive checksum: $partialPath"
            }
            Move-Item -LiteralPath $partialPath -Destination $archivePath -Force
        }
        finally {
            Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
        }
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDirectory -Force
    $executable = (Resolve-Path -LiteralPath (Join-Path $runtimeDirectory $engine.Executable)).Path
    Set-Item -Path ('Env:' + $engine.Variable) -Value $executable
    if ($env:GITHUB_ENV) {
        "$($engine.Variable)=$executable" | Out-File -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Append
    }
}

Write-Host 'Pinned sing-box and Xray are ready for integration tests.'
