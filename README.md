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
   Ctrl+V accepts config text or one copied file and rejects unsupported formats
   before loading. Normal text paste still works in connection fields. One local
   file can also be dropped anywhere in the main window.
3. Converts a supported proxy outbound to the sing-box format.
4. Shows the current and proposed outbound while masking passwords, UUIDs, and
   private keys. Fields use a consistent, documentation-style order in the
   preview and the replaced outbound.
5. Validates a temporary configuration with `sing-box check`.
6. Creates a temporary backup on the router after validation succeeds.
7. Installs the configuration atomically and restarts the OpenWrt service.
8. Verifies the service and automatically rolls back on failure.
9. Downloads verified SingRoute updates, replaces the portable executable after
   shutdown, and launches the updated application.

SingRoute supports native sing-box outbounds, HAPP/Xray VLESS Reality over TCP
or gRPC, HAPP/Xray Trojan over TCP/TLS or TCP/Reality, and HAPP Hysteria2.
Trojan and VLESS Reality gRPC also accept the standard gRPC transport.
Converted VLESS outbounds allow both TCP and UDP traffic; sing-box chooses its
default UDP packet encoding. For VLESS Reality with a regular gRPC service name,
Xray `multiMode: true` is converted to standard gRPC: Xray servers expose both
`Tun` and `TunMulti` methods. Custom gRPC paths, non-Reality `multiMode`, and a
nonempty `authority` are rejected when they cannot be converted safely.
XHTTP profiles require an XHTTP-capable router core; stock sing-box cannot use
them. For a router running stock sing-box, request a VLESS Reality TCP or gRPC
profile for the same server from the provider. Changing only the transport name
in the exported JSON will not make the connection work.

## Portable usage

Download and run the single file:

```text
SingRoute.exe
```

Opening the same copy again brings its existing window to the foreground,
restoring it if minimized.

Version 0.5.0 adds file drag and drop, imports a copied config file with Ctrl+V,
and identifies unsupported VLESS transports in the import message. It also
requires a valid active router outbound before replacement.
Version 0.5.1 fixes HAPP/Xray VLESS conversion so TCP transport no longer
restricts the sing-box outbound to TCP traffic.
Version 0.6.0 adds HAPP/Xray Trojan and gRPC import, orders outbound fields in
the preview, and lets you confirm a changed router SSH host key after checking
its fingerprint on the router.
Version 0.6.1 imports HAPP/Xray Reality gRPC profiles with `multiMode: true`
when a regular service name can use the server's standard `Tun` method.

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
confirmation, the portable build downloads the exact official executable. Its
SHA-256 hash must match the digest published by GitHub before installation.

The verified file is staged next to `SingRoute.exe`. A background Windows helper
then closes SingRoute, waits for all PyInstaller file locks to disappear,
replaces the executable with rollback protection, and starts SingRoute again.
Version 0.3.3 adds this complete automatic replacement and restart flow.
Version 0.3.5 fixes helper startup from the frozen application and requires a
readiness signal before SingRoute closes, preventing silent failed updates.
Version 0.4.0 keeps the previous executable until the updated GUI reports
readiness and remains stable. A failed update is stopped, rolled back, and the
restored version is checked after restart. It also starts both executables in an
independent PyInstaller environment so they cannot reuse the old process's
already-removed temporary extraction. Releases deliberately do not publish the
legacy `.sha256` sidecar: SingRoute 0.3.5 and older must not enter their unsafe
automatic replacement path.

Version 0.4.0 improves updater diagnostics, reports retained rollback files that
would block the next automatic update, and distinguishes application-update
operations from router configuration changes in the interface.

**The first upgrade from SingRoute 0.3.5 or older to 0.4.0 must be manual:** close
all SingRoute processes, download the official `SingRoute.exe`, and replace the
old file under that exact name. Do not leave the new version beside it under a
different name. Installation logic always comes from the version that is already
running, so only later updates started from 0.4.0 receive the new health check
and rollback protection.

Default connection settings:

- router: `192.168.1.1`;
- SSH user: `root`;
- SSH port: `22`;
- configuration: `/etc/sing-box/config.json`;
- service: `/etc/init.d/sing-box`.

The main connection row contains the address, user, password, and password
storage option. The eye button in the password field temporarily reveals or
hides the entered password. SSH port, authentication mode, private key,
configuration path, and service name are available under advanced settings.

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
trust source for that router. If a router reset changes its key, the app shows
the new SHA256 fingerprint and requires fresh confirmation before connecting.
Compare it directly on the router (for Dropbear, for example:
`dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key`; use the host key
file matching the algorithm shown in the dialog). The connection remains
blocked until confirmation. Password-only authentication does not bypass the
router host key check.

Version 0.3.1 also uses a private randomized remote operation directory, an
atomic update lock held through service verification or rollback, and a hash
comparison immediately before installation. Replacement preserves the original
config owner and mode. SingRoute checks service status immediately and again
after a stability delay before deleting the backup. It reconciles ambiguous SSH
failures before deciding whether to restart or roll back, limits imported
configuration and SSH I/O to 8 MiB, masks a broader set of secret fields, and
removes stale Windows credentials when connection identity changes.
Configuration installation cannot be cancelled halfway through, preserving the
automatic rollback path.

## Development

SingRoute requires Python 3.11–3.14 and Poetry.

```powershell
poetry config virtualenvs.in-project true --local
poetry install
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q --cov=singroute --cov-report=term-missing
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

Verify the packaged executable and its complete replacement/rollback lifecycle:

```powershell
.\scripts\e2e_test_portable_update.ps1 dist\SingRoute.exe
```

The E2E test builds a temporary frozen previous version, streams the production
EXE through the real download/staging code, installs it through the real Windows
helper, and confirms that the new GUI starts and remains running. Beforehand it
extracts `update.ps1` from the production PyInstaller archive and compares it
byte-for-byte with the source resource. It then installs a deliberately
unsuitable EXE and confirms that the previous frozen version is restored and
running. All test executables and processes live in a unique temporary
directory.

## Release

The project version is defined in `pyproject.toml`. Create and push a matching
tag to publish a GitHub Release:

```powershell
git tag -a v0.7.0 -m "Release v0.7.0"
git push origin v0.7.0
```

The `Release` workflow validates metadata and formatting, audits Python
dependencies, runs the Windows test suite with branch coverage (minimum 70%),
builds `SingRoute.exe`, runs both packaged executable tests, generates its
build attestation, and publishes the executable to GitHub Releases. A tag that
does not match the project version fails before publication. Current versions
verify the SHA-256 digest supplied by GitHub directly; legacy checksum sidecars
are intentionally omitted so SingRoute 0.3.5 and older require a manual first
upgrade.

## License

SingRoute is available under the [MIT License](LICENSE).
