from datetime import date
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

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


class PageLocator(DomainModel):
    locator_type: Literal["page"] = "page"
    page: int = Field(ge=1)


class SlideLocator(DomainModel):
    locator_type: Literal["slide"] = "slide"
    slide: int = Field(ge=1)


class SectionLocator(DomainModel):
    locator_type: Literal["section"] = "section"
    heading: str = Field(min_length=1)


class ChatMessageLocator(DomainModel):
    locator_type: Literal["chat_message"] = "chat_message"
    message_id: str = Field(min_length=1)


class FigureLocator(DomainModel):
    locator_type: Literal["figure"] = "figure"
    label: str = Field(min_length=1)


class TableLocator(DomainModel):
    locator_type: Literal["table"] = "table"
    label: str = Field(min_length=1)


SourceLocator = Annotated[
    PageLocator | SlideLocator | SectionLocator | ChatMessageLocator | FigureLocator | TableLocator,
    Field(discriminator="locator_type"),
]


class SourceDocument(DomainModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    source_id: str = Field(pattern=r"^source_[a-f0-9]{32}$")
    source_type: Literal[
        "textbook", "guideline", "course_slide", "paper", "question_bank", "learning_chat", "web"
    ]
    title: str = Field(min_length=1)
    authority: int = Field(ge=0, le=5)
    publication_date: date | None = None
    version: str | None = None
    vault_path: VaultPath | None = None


class SourceCitation(DomainModel):
    source_id: str = Field(pattern=r"^source_[a-f0-9]{32}$")
    locator: SourceLocator
    quotation: str | None = None
