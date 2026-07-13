import sys

import pytest

from medlearn_vault.sync_models import SyncError
from medlearn_vault.windows_secrets import delete_token, load_token, store_token


def test_env_token_override(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDLEARN_SYNC_TOKEN", "x" * 32)
    assert load_token(tmp_path / "credential.bin") == "x" * 32


def test_short_env_token_is_rejected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDLEARN_SYNC_TOKEN", "short")
    with pytest.raises(SyncError, match="SYNC_CREDENTIAL_FAILURE"):
        load_token(tmp_path / "credential.bin")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows feature")
def test_dpapi_round_trip_is_not_plaintext(tmp_path) -> None:
    path = tmp_path / "credential.bin"
    token = "x" * 32
    store_token(path, token)
    assert token.encode() not in path.read_bytes()
    assert load_token(path) == token
    delete_token(path)
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows feature")
def test_dpapi_damaged_ciphertext_fails_closed(tmp_path) -> None:
    path = tmp_path / "credential.bin"
    path.write_bytes(b"damaged")
    with pytest.raises(SyncError, match="SYNC_CREDENTIAL_FAILURE"):
        load_token(path)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has DPAPI")
def test_non_windows_requires_env_token(tmp_path) -> None:
    with pytest.raises(SyncError, match="SYNC_UNSUPPORTED_PLATFORM"):
        load_token(tmp_path / "credential.bin")
