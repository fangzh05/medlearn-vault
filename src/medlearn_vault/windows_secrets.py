"""Current-user Windows DPAPI storage; tokens never leave this module as text on disk."""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from medlearn_vault.sync_models import SyncError

_ENTROPY = b"medlearn-vault-sync-token-v1"
_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(value: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(value: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    data, data_buffer = _blob(value)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _Blob()
    windll: Any = vars(ctypes)["windll"]
    crypt = windll.crypt32
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    description = ctypes.c_wchar_p()
    if protect:
        ok = fn(
            ctypes.byref(data),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(output),
        )
    else:
        ok = fn(
            ctypes.byref(data),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(output),
        )
    del data_buffer, entropy_buffer
    try:
        if not ok:
            raise SyncError("SYNC_CREDENTIAL_FAILURE")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            windll.kernel32.LocalFree(output.pbData)
        if description.value is not None:
            windll.kernel32.LocalFree(description)


def store_token(path: Path, token: str) -> None:
    if len(token) < 32:
        raise SyncError("SYNC_CREDENTIAL_FAILURE")
    path.parent.mkdir(parents=True, exist_ok=True)
    ciphertext = _protect(token.encode("utf-8"), True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(ciphertext)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_token(path: Path) -> str:
    override = os.environ.get("MEDLEARN_SYNC_TOKEN")
    if override is not None:
        if len(override) < 32:
            raise SyncError("SYNC_CREDENTIAL_FAILURE")
        return override
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    if not path.is_file():
        raise SyncError("SYNC_NOT_AUTHENTICATED")
    try:
        token = _protect(path.read_bytes(), False).decode("utf-8")
        if len(token) < 32:
            raise SyncError("SYNC_CREDENTIAL_FAILURE")
        return token
    except (OSError, UnicodeDecodeError, SyncError) as exc:
        if isinstance(exc, SyncError) and exc.code != "SYNC_CREDENTIAL_FAILURE":
            raise
        raise SyncError("SYNC_CREDENTIAL_FAILURE") from exc


def delete_token(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
