from pathlib import Path


def test_release_entrypoint_has_safe_explicit_gates() -> None:
    source = Path("scripts/release_windows.ps1").read_text(encoding="utf-8")
    assert "[switch] $ValidateOnly" in source
    assert "[switch] $SkipProduction" in source
    assert "Read-Host 'Type MIGRATE_PRODUCTION" in source
    assert "install_windows_client.ps1" in source
    assert "windows_sync_rollout.ps1" in source
    assert "wrangler','deploy','--dry-run" in source
    assert "npm.cmd','run','deploy" in source


def test_release_entrypoint_never_accepts_secret_arguments() -> None:
    source = Path("scripts/release_windows.ps1").read_text(encoding="utf-8")
    params = source.split("$ErrorActionPreference", 1)[0]
    assert "Token" not in params
    assert "Secret" not in params
    assert "Password" not in params
    assert "SYNC_RELEASE_SECRET_ARGUMENT_FORBIDDEN" in source
