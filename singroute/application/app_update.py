"""Release notification without executing remotely supplied binaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LATEST_RELEASE_API = "https://api.github.com/repos/oleg4bat/singroute/releases/latest"
MAX_METADATA_BYTES = 1_048_576
REQUEST_TIMEOUT_SECONDS = 15
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_RELEASE_PAGE_PREFIX = "https://github.com/oleg4bat/singroute/releases/tag/"


class AppUpdateError(RuntimeError):
    """Raised when release metadata cannot be checked safely."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    page_url: str


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_release: ReleaseInfo
    update_available: bool


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


def _parse_release(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise AppUpdateError("GitHub вернул некорректное описание релиза.")
    tag = payload.get("tag_name")
    page_url = payload.get("html_url")
    if not isinstance(tag, str) or not isinstance(page_url, str):
        raise AppUpdateError("В описании релиза отсутствуют обязательные поля.")
    version_tuple = _parse_version(tag, "тега релиза")
    if page_url != f"{_RELEASE_PAGE_PREFIX}{tag}":
        raise AppUpdateError("GitHub вернул неожиданный адрес страницы релиза.")
    version = ".".join(str(part) for part in version_tuple)
    return ReleaseInfo(version=version, tag=tag, page_url=page_url)


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
