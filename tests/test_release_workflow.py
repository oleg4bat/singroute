from pathlib import Path


def test_release_does_not_enable_the_legacy_unsafe_updater():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "create_release_checksum" not in workflow
    assert "SingRoute.exe.sha256" not in workflow
