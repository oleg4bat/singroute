# SingRoute

[English](README.md) | [Русский](README_RU.md)

Safe portable Windows application for synchronizing proxy outbounds from HAPP
or NekoBox exports with a sing-box configuration on an OpenWrt router.

> SingRoute is an independent, unofficial project. It is not affiliated with,
> sponsored by, or endorsed by SagerNet or the sing-box project. sing-box is
> referenced only to describe compatibility.

## Features

1. Connects to OpenWrt over SSH and keeps the session for subsequent actions.
2. Imports JSON from the clipboard or a HAPP/NekoBox configuration file.
3. Converts a supported proxy outbound to the sing-box format.
4. Shows the current and proposed outbound while masking passwords, UUIDs, and
   private keys.
5. Validates a temporary configuration with `sing-box check`.
6. Creates a temporary backup on the router after validation succeeds.
7. Installs the configuration atomically and restarts the OpenWrt service.
8. Verifies the service and automatically rolls back on failure.
9. Downloads verified SingRoute updates, replaces the portable executable after
   shutdown, and launches the updated application.

SingRoute supports native sing-box outbounds, HAPP/Xray VLESS Reality over TCP,
and HAPP Hysteria2.

## Portable usage

Download and run the single file:

```text
SingRoute.exe
```

Python and an installer are not required. After settings are saved, SingRoute
creates this file next to the executable:

```text
SingRoute.ini
```

When upgrading from `v0.1.0`, SingRoute automatically reads the legacy
`singbox-outbound-updater.ini` file and migrates stored Windows credentials when
they are first used. Imported configuration text is never persisted because it
may contain secrets.

## Updating SingRoute

By default, SingRoute checks the official latest GitHub release at startup.
Startup checks can be disabled in advanced settings, and a manual check remains
available from the button at the top of the main window. After explicit user
confirmation, the portable build downloads the exact official executable and
checksum assets. It requires the checksum, GitHub asset digest, and downloaded
file hash to agree before installation.

The verified file is staged next to `SingRoute.exe`. A detached Windows helper
then closes SingRoute, waits for all PyInstaller file locks to disappear,
replaces the executable with rollback protection, and starts SingRoute again.
Version 0.3.3 adds this complete automatic replacement and restart flow.

Default connection settings:

- router: `192.168.1.1`;
- SSH user: `root`;
- SSH port: `22`;
- configuration: `/etc/sing-box/config.json`;
- service: `/etc/init.d/sing-box`.

The main connection row contains the address, user, password, and password
storage option. SSH port, authentication mode, private key, configuration path,
and service name are available under advanced settings.

## SSH and safety

Automatic authentication tries SSH agent keys, standard keys from
`%USERPROFILE%\.ssh`, `IdentityFile` entries from SSH config, and finally the
entered password. A custom private key can be selected without copying it.
This is the user's private authentication key and remains on Windows; it is not
the router host key described below.

Passwords and key passphrases are never written to the INI file. With password
storage enabled, the password is kept in Windows Credential Manager and scoped
to the router user, address, and port.

An unknown router SSH host key requires explicit fingerprint confirmation. The
confirmed public key is stored in the INI file and is the application's only
trust source for that router; a later key change blocks the connection.

Version 0.3.1 also uses a private randomized remote operation directory, an
atomic update lock, and a hash comparison immediately before installation. It
reconciles ambiguous SSH failures before deciding whether to restart or roll
back, limits imported configuration and SSH I/O to 8 MiB, masks a broader set
of secret fields, and removes stale Windows credentials when connection
identity changes. Configuration installation cannot be cancelled halfway
through, preserving the automatic rollback path.

## Development

SingRoute requires Python 3.11–3.14 and Poetry.

```powershell
poetry config virtualenvs.in-project true --local
poetry install
poetry run pytest
poetry run pip-audit
poetry run python main.py
```

## Build

```powershell
.\build.ps1
```

The portable executable is written to:

```text
dist\SingRoute.exe
```

## Release

The project version is defined in `pyproject.toml`. Create and push a matching
tag to publish a GitHub Release:

```powershell
git tag -a v0.3.3 -m "Release v0.3.3"
git push origin v0.3.3
```

The `Release` workflow validates the metadata, audits Python dependencies, runs
the Windows test suite, builds `SingRoute.exe`, generates its SHA-256 checksum
and build attestation, and publishes the files to GitHub Releases. A tag that
does not match the project version fails before publication.

## License

SingRoute is available under the [MIT License](LICENSE).
