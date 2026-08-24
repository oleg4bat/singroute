"""Secure update workflow for the portable Windows executable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LATEST_RELEASE_API = "https://api.github.com/repos/oleg4bat/singroute/releases/latest"
EXECUTABLE_ASSET_NAME = "SingRoute.exe"
CHECKSUM_ASSET_NAME = "SingRoute.exe.sha256"
MAX_METADATA_BYTES = 1_048_576
MAX_EXECUTABLE_BYTES = 250 * 1_048_576
REQUEST_TIMEOUT_SECONDS = 15
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_CHECKSUM_PATTERN = re.compile(
    rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(EXECUTABLE_ASSET_NAME)}$",
    re.MULTILINE,
)


class AppUpdateError(RuntimeError):
    """Raised when an application update cannot be checked or prepared safely."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    page_url: str
    executable_url: str
    checksum_url: str


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
    """Download and verify an update next to the current portable executable."""
    target_path = target_path.resolve()
    if target_path.name.casefold() != EXECUTABLE_ASSET_NAME.casefold():
        raise AppUpdateError(
            "Автоматическое обновление доступно только для portable SingRoute.exe."
        )
    if not target_path.is_file():
        raise AppUpdateError(f"Текущий файл программы не найден: {target_path}")

    checksum_text = _read_bytes(
        release.checksum_url,
        MAX_METADATA_BYTES,
        opener,
    )
    try:
        checksum_text = checksum_text.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise AppUpdateError("Файл SHA-256 релиза имеет неожиданный формат.") from error
    checksum_match = _CHECKSUM_PATTERN.search(checksum_text)
    if checksum_match is None:
        raise AppUpdateError("Файл SHA-256 релиза имеет неожиданный формат.")
    expected_digest = checksum_match.group(1).lower()

    staged_path = target_path.with_name(
        f".{target_path.stem}.update-v{release.version}{target_path.suffix}"
    )
    partial_path = staged_path.with_suffix(staged_path.suffix + ".part")
    staged_path.unlink(missing_ok=True)
    try:
        actual_digest = _download_file(
            release.executable_url,
            partial_path,
            MAX_EXECUTABLE_BYTES,
            opener,
        )
        if actual_digest != expected_digest:
            raise AppUpdateError(
                "SHA-256 загруженного обновления не совпадает с опубликованной "
                "контрольной суммой. Файл удалён."
            )
        os.replace(partial_path, staged_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return StagedUpdate(
        release=release,
        executable_path=staged_path,
        target_path=target_path,
    )


def launch_staged_update(staged: StagedUpdate, process_id: int | None = None) -> None:
    """Launch a detached helper that replaces the executable after this process exits."""
    if sys.platform != "win32":
        raise AppUpdateError("Автоматическая установка поддерживается только в Windows.")
    if not staged.executable_path.is_file():
        raise AppUpdateError(f"Загруженное обновление не найдено: {staged.executable_path}")

    powershell = _powershell_executable()
    script_path = _write_installer_script()
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-SingRouteProcessId",
        str(process_id or os.getpid()),
        "-StagedPath",
        str(staged.executable_path),
        "-TargetPath",
        str(staged.target_path),
        "-ScriptPath",
        str(script_path),
    ]
    try:
        subprocess.Popen(
            command,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS,
        )
    except OSError as error:
        script_path.unlink(missing_ok=True)
        raise AppUpdateError(f"Не удалось запустить установщик обновления: {error}") from error


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
    version = ".".join(str(part) for part in version_tuple)

    asset_urls: dict[str, str] = {}
    download_prefix = (
        f"https://github.com/oleg4bat/singroute/releases/download/{tag}/"
    )
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if (
            isinstance(name, str)
            and isinstance(url, str)
            and url.startswith(download_prefix)
        ):
            asset_urls[name] = url
    try:
        executable_url = asset_urls[EXECUTABLE_ASSET_NAME]
        checksum_url = asset_urls[CHECKSUM_ASSET_NAME]
    except KeyError as error:
        raise AppUpdateError(
            "В последнем релизе отсутствуют SingRoute.exe или его SHA-256."
        ) from error
    if not page_url.startswith("https://github.com/oleg4bat/singroute/"):
        raise AppUpdateError("GitHub вернул неожиданный адрес страницы релиза.")
    return ReleaseInfo(version, tag, page_url, executable_url, checksum_url)


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
        with _open_url(url, opener) as response, destination.open("wb") as stream:
            _copy_and_hash(response, stream, digest, limit, total)
    except (HTTPError, URLError, OSError) as error:
        raise AppUpdateError(f"Не удалось загрузить обновление: {error}") from error
    return digest.hexdigest()


def _copy_and_hash(
    source: BinaryIO,
    destination: BinaryIO,
    digest: Any,
    limit: int,
    total: int,
) -> None:
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
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
    [Parameter(Mandatory=$true)][string]$ScriptPath
)
$ErrorActionPreference = "Stop"
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (Get-Process -Id $SingRouteProcessId -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) { exit 2 }
        Start-Sleep -Milliseconds 250
    }
    Move-Item -Force -LiteralPath $StagedPath -Destination $TargetPath
    Start-Process -FilePath $TargetPath
}
finally {
    Remove-Item -Force -LiteralPath $ScriptPath -ErrorAction SilentlyContinue
}
'''
