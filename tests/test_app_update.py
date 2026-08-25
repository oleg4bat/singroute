from __future__ import annotations

from io import BytesIO
import json
from urllib.request import Request

import pytest

from singroute.application.app_update import (
    AppUpdateError,
    LATEST_RELEASE_API,
    MAX_METADATA_BYTES,
    check_for_update,
)


class FakeResponse(BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> FakeResponse:
        assert timeout > 0
        self.requests.append(request)
        return FakeResponse(self.responses[request.full_url])


def test_check_for_update_finds_newer_official_release():
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.3.1")})

    result = check_for_update("0.3.0", opener=opener)

    assert result.update_available is True
    assert result.latest_release.version == "0.3.1"
    assert result.latest_release.tag == "v0.3.1"
    assert result.latest_release.page_url.endswith("/tag/v0.3.1")
    assert opener.requests[0].get_header("User-agent") == "SingRoute-Updater"


def test_check_for_update_does_not_downgrade():
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("v0.3.0")})

    result = check_for_update("0.3.1", opener=opener)

    assert result.update_available is False


def test_check_rejects_release_page_from_unexpected_location():
    payload = json.loads(_release_json("v0.3.1"))
    payload["html_url"] = "https://example.test/releases/tag/v0.3.1"
    opener = FakeOpener({LATEST_RELEASE_API: json.dumps(payload).encode()})

    with pytest.raises(AppUpdateError, match="неожиданный адрес"):
        check_for_update("0.3.0", opener=opener)


def test_check_rejects_non_semantic_release_tag():
    opener = FakeOpener({LATEST_RELEASE_API: _release_json("latest")})

    with pytest.raises(AppUpdateError, match="формат"):
        check_for_update("0.3.0", opener=opener)


def test_check_rejects_oversized_metadata():
    opener = FakeOpener({LATEST_RELEASE_API: b"x" * (MAX_METADATA_BYTES + 1)})

    with pytest.raises(AppUpdateError, match="превышает"):
        check_for_update("0.3.0", opener=opener)


def _release_json(tag: str) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "html_url": f"https://github.com/oleg4bat/singroute/releases/tag/{tag}",
        }
    ).encode("utf-8")
