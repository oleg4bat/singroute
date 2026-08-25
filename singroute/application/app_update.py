"""Verified updates for the portable Windows executable."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LATEST_RELEASE_API = "https://api.github.com/repos/oleg4bat/singroute/releases/latest"
EXECUTABLE_ASSET_NAME = "SingRoute.exe"
CHECKSUM_ASSET_NAME = "SingRoute.exe.sha256"
MAX_METADATA_BYTES = 1_048_576
MAX_EXECUTABLE_BYTES = 250 * 1_048_576
REQUEST_TIMEOUT_SECONDS = 15
HELPER_START_TIMEOUT_SECONDS = 5
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_CHECKSUM_PATTERN = re.compile(
    rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(EXECUTABLE_ASSET_NAME)}$"
)
_RELEASE_PAGE_PREFIX = "https://github.com/oleg4bat/singroute/releases/tag/"
_RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/oleg4bat/singroute/releases/download/"
)


class AppUpdateError(RuntimeError):
    """Raised when an application update cannot be completed safely."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    page_url: str
    executable_url: str
    checksum_url: str
    executable_digest: str


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_release: ReleaseInfo
    update_available: bool


@dataclass(frozen=True)
class StagedUpdate:
    release: ReleaseInfo
    executable_path: Path
    target_path: Path
    expected_digest: str


UrlOpener = Callable[..., Any]


def check_for_update(
    current_version: str,
    *,
    opener: UrlOpener = urlopen,
) -> UpdateCheckResult:
    """Return the latest official GitHub release and comparison result."""
    current = _parse_version(current_version, "текущей версии")
    payload = _read_json(LATEST_RELEASE_API, opener)
    release = _parse_release(payload)
    latest = _parse_version(release.version, "версии релиза")
    return UpdateCheckResult(
        current_version=current_version,
        latest_release=release,
        update_available=latest > current,
    )


def stage_update(
    release: ReleaseInfo,
    target_path: Path,
    *,
    opener: UrlOpener = urlopen,
) -> StagedUpdate:
    """Download and verify an update next to the running portable executable."""
    target_path = target_path.resolve()
    if target_path.name.casefold() != EXECUTABLE_ASSET_NAME.casefold():
        raise AppUpdateError(
            "Автоматическое обновление доступно только для portable SingRoute.exe."
        )
    if not target_path.is_file():
        raise AppUpdateError(f"Текущий файл программы не найден: {target_path}")

    checksum_bytes = _read_bytes(
        release.checksum_url,
        MAX_METADATA_BYTES,
        opener,
    )
    expected_digest = _parse_checksum(checksum_bytes)
    if expected_digest != release.executable_digest:
        raise AppUpdateError(
            "Контрольная сумма релиза не совпадает с digest, опубликованным GitHub."
        )

    staged_path = target_path.with_name(
        f".{target_path.stem}.update-v{release.version}{target_path.suffix}"
    )
    partial_path = staged_path.with_suffix(staged_path.suffix + ".part")
    staged_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)
    try:
        actual_digest = _download_file(
            release.executable_url,
            partial_path,
            MAX_EXECUTABLE_BYTES,
            opener,
        )
        if actual_digest != expected_digest:
            raise AppUpdateError(
                "Загруженное обновление не прошло проверку целостности. Файл удалён."
            )
        with partial_path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise AppUpdateError(
                    "Загруженное обновление не является Windows-приложением."
                )
        os.replace(partial_path, staged_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        staged_path.unlink(missing_ok=True)
        raise

    return StagedUpdate(
        release=release,
        executable_path=staged_path,
        target_path=target_path,
        expected_digest=expected_digest,
    )


def launch_staged_update(staged: StagedUpdate, process_id: int | None = None) -> None:
    """Launch a helper that replaces the EXE after all locks disappear."""
    if sys.platform != "win32":
        raise AppUpdateError("Автоматическая установка поддерживается только в Windows.")
    expected_staged_path = staged.target_path.with_name(
        f".{staged.target_path.stem}.update-v{staged.release.version}"
        f"{staged.target_path.suffix}"
    )
    if staged.executable_path.resolve() != expected_staged_path.resolve():
        raise AppUpdateError("Некорректный путь подготовленного обновления.")
    if not staged.executable_path.is_file():
        raise AppUpdateError(f"Загруженное обновление не найдено: {staged.executable_path}")
    if _hash_file(staged.executable_path) != staged.expected_digest:
        staged.executable_path.unlink(missing_ok=True)
        raise AppUpdateError(
            "Подготовленное обновление изменилось после загрузки и было удалено."
        )

    backup_path = _backup_path(staged.target_path)
    error_path = _error_path(staged.target_path)
    ready_path = staged.target_path.with_name(".SingRoute.update-ready")
    if backup_path.exists():
        raise AppUpdateError(
            f"Обнаружена резервная копия прошлого обновления: {backup_path}. "
            "Проверьте её перед повторной установкой."
        )
    error_path.unlink(missing_ok=True)
    ready_path.unlink(missing_ok=True)
    script_path = _write_installer_script()
    command = [
        str(_powershell_executable()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
        "-SingRouteProcessId",
        str(process_id or os.getpid()),
        "-StagedPath",
        str(staged.executable_path),
        "-TargetPath",
        str(staged.target_path),
        "-BackupPath",
        str(backup_path),
        "-ErrorPath",
        str(error_path),
        "-ReadyPath",
        str(ready_path),
        "-ScriptPath",
        str(script_path),
    ]
    try:
        helper_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as error:
        with suppress(OSError):
            script_path.unlink(missing_ok=True)
        raise AppUpdateError(f"Не удалось запустить установщик обновления: {error}") from error

    deadline = time.monotonic() + HELPER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        exit_code = helper_process.poll()
        if exit_code is not None:
            with suppress(OSError):
                script_path.unlink(missing_ok=True)
            raise AppUpdateError(
                "Установщик обновления не смог запуститься "
                f"(код {exit_code}). SingRoute остаётся открытым."
            )
        time.sleep(0.05)

    with suppress(OSError):
        helper_process.terminate()
    try:
        helper_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            helper_process.kill()
        helper_process.wait(timeout=2)
    with suppress(OSError):
        ready_path.unlink(missing_ok=True)
    with suppress(OSError):
        script_path.unlink(missing_ok=True)
    raise AppUpdateError(
        "Установщик обновления не подтвердил запуск. "
        "SingRoute остаётся открытым."
    )


def is_portable_windows_build() -> bool:
    return (
        sys.platform == "win32"
        and bool(getattr(sys, "frozen", False))
        and Path(sys.executable).name.casefold() == EXECUTABLE_ASSET_NAME.casefold()
    )


def current_executable_path() -> Path:
    return Path(sys.executable).resolve()


def _parse_release(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise AppUpdateError("GitHub вернул некорректное описание релиза.")
    tag = payload.get("tag_name")
    page_url = payload.get("html_url")
    assets = payload.get("assets")
    if not isinstance(tag, str) or not isinstance(page_url, str) or not isinstance(assets, list):
        raise AppUpdateError("В описании релиза отсутствуют обязательные поля.")
    version_tuple = _parse_version(tag, "тега релиза")
    if page_url != f"{_RELEASE_PAGE_PREFIX}{tag}":
        raise AppUpdateError("GitHub вернул неожиданный адрес страницы релиза.")

    download_prefix = f"{_RELEASE_DOWNLOAD_PREFIX}{tag}/"
    asset_data: dict[str, tuple[str, str | None]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        digest = asset.get("digest")
        if (
            isinstance(name, str)
            and isinstance(url, str)
            and url == f"{download_prefix}{name}"
            and name not in asset_data
        ):
            asset_data[name] = (url, digest if isinstance(digest, str) else None)
    try:
        executable_url, raw_digest = asset_data[EXECUTABLE_ASSET_NAME]
        checksum_url, _ = asset_data[CHECKSUM_ASSET_NAME]
    except KeyError as error:
        raise AppUpdateError(
            "В последнем релизе отсутствуют SingRoute.exe или файл проверки."
        ) from error
    digest_match = _DIGEST_PATTERN.fullmatch(raw_digest or "")
    if digest_match is None:
        raise AppUpdateError("GitHub не опубликовал корректный digest обновления.")

    version = ".".join(str(part) for part in version_tuple)
    return ReleaseInfo(
        version=version,
        tag=tag,
        page_url=page_url,
        executable_url=executable_url,
        checksum_url=checksum_url,
        executable_digest=digest_match.group(1).lower(),
    )


def _parse_checksum(value: bytes) -> str:
    try:
        text = value.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise AppUpdateError("Файл проверки релиза имеет неожиданный формат.") from error
    lines = [line for line in text.splitlines() if line]
    if len(lines) != 1:
        raise AppUpdateError("Файл проверки релиза имеет неожиданный формат.")
    match = _CHECKSUM_PATTERN.fullmatch(lines[0])
    if match is None:
        raise AppUpdateError("Файл проверки релиза имеет неожиданный формат.")
    return match.group(1).lower()


def _parse_version(value: str, label: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise AppUpdateError(f"Некорректный формат {label}: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _read_json(url: str, opener: UrlOpener) -> Any:
    raw = _read_bytes(url, MAX_METADATA_BYTES, opener)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppUpdateError("GitHub вернул некорректный JSON релиза.") from error


def _read_bytes(url: str, limit: int, opener: UrlOpener) -> bytes:
    try:
        with _open_url(url, opener) as response:
            data = response.read(limit + 1)
    except (HTTPError, URLError, OSError) as error:
        raise AppUpdateError(f"Не удалось загрузить данные обновления: {error}") from error
    if len(data) > limit:
        raise AppUpdateError("Ответ сервера обновлений превышает допустимый размер.")
    return data


def _download_file(
    url: str,
    destination: Path,
    limit: int,
    opener: UrlOpener,
) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with _open_url(url, opener) as response, destination.open("xb") as stream:
            total = _copy_and_hash(response, stream, digest, limit, total)
    except (HTTPError, URLError, OSError) as error:
        raise AppUpdateError(f"Не удалось загрузить обновление: {error}") from error
    return digest.hexdigest()


def _copy_and_hash(
    source: BinaryIO,
    destination: BinaryIO,
    digest: Any,
    limit: int,
    total: int,
) -> int:
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise AppUpdateError("Файл обновления превышает допустимый размер.")
        digest.update(chunk)
        destination.write(chunk)


def _open_url(url: str, opener: UrlOpener) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SingRoute-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return opener(request, timeout=REQUEST_TIMEOUT_SECONDS)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_path(target_path: Path) -> Path:
    return target_path.with_name(".SingRoute.previous.exe")


def _error_path(target_path: Path) -> Path:
    return target_path.with_name("SingRoute-update-error.txt")


def _powershell_executable() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise AppUpdateError("Не найден системный Windows PowerShell для установки обновления.")
    return executable


def _write_installer_script() -> Path:
    handle, raw_path = tempfile.mkstemp(prefix="SingRoute-update-", suffix=".ps1")
    os.close(handle)
    path = Path(raw_path)
    try:
        path.write_text(_INSTALLER_SCRIPT, encoding="utf-8-sig")
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


_INSTALLER_SCRIPT = r'''param(
    [Parameter(Mandatory=$true)][int]$SingRouteProcessId,
    [Parameter(Mandatory=$true)][string]$StagedPath,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][string]$BackupPath,
    [Parameter(Mandatory=$true)][string]$ErrorPath,
    [Parameter(Mandatory=$true)][string]$ReadyPath,
    [Parameter(Mandatory=$true)][string]$ScriptPath
)
$ErrorActionPreference = "Stop"
[System.IO.File]::WriteAllText($ReadyPath, "ready")

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

$backupCreated = $false
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (Get-Process -Id $SingRouteProcessId -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "SingRoute did not exit before the update deadline."
        }
        Start-Sleep -Milliseconds 250
    }

    if (Test-Path -LiteralPath $BackupPath) {
        throw "A previous update backup already exists: $BackupPath"
    }
    Move-WithRetry -Source $TargetPath -Destination $BackupPath -Deadline $deadline
    $backupCreated = $true

    try {
        Move-Item -LiteralPath $StagedPath -Destination $TargetPath -ErrorAction Stop
    }
    catch {
        Move-Item -LiteralPath $BackupPath -Destination $TargetPath -ErrorAction Stop
        $backupCreated = $false
        throw
    }

    try {
        Start-Process -FilePath $TargetPath -ErrorAction Stop
    }
    catch {
        Move-Item -LiteralPath $TargetPath -Destination $StagedPath -ErrorAction Stop
        Move-Item -LiteralPath $BackupPath -Destination $TargetPath -ErrorAction Stop
        $backupCreated = $false
        throw
    }

    Remove-Item -Force -LiteralPath $BackupPath -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $ErrorPath -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $failure = $_.Exception.ToString()
    try {
        if ($backupCreated -and -not (Test-Path -LiteralPath $TargetPath)) {
            Move-Item -LiteralPath $BackupPath -Destination $TargetPath -ErrorAction Stop
            $backupCreated = $false
        }
        if (Test-Path -LiteralPath $TargetPath) {
            Start-Process -FilePath $TargetPath -ErrorAction SilentlyContinue
        }
    }
    finally {
        try {
            [System.IO.File]::WriteAllText(
                $ErrorPath,
                $failure,
                [System.Text.UTF8Encoding]::new($false)
            )
        }
        catch {}
    }
    exit 1
}
finally {
    Remove-Item -Force -LiteralPath $ReadyPath -ErrorAction SilentlyContinue
    Remove-Item -Force -LiteralPath $ScriptPath -ErrorAction SilentlyContinue
}
'''
