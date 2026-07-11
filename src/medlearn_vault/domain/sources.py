from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import BeforeValidator

from medlearn_vault.domain.base import DomainModel


def _vault_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Vault path must be a non-empty string")
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
        raise ValueError("Vault path must be relative and cannot contain '..'")
    return path.as_posix()


VaultPath = Annotated[str, BeforeValidator(_vault_path)]


class SourceCitation(DomainModel):
    source_id: str
    locator: str
    vault_path: VaultPath | None = None
    quotation: str | None = None
