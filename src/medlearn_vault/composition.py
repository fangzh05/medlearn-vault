"""Local, deterministic note-composition preview from persisted intake data."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import ValidationError

from medlearn_vault.capture import IntakeEnvelope
from medlearn_vault.handoff import LearningSegment, MedLearnHandoff


@dataclass(frozen=True)
class CompositionIssue:
    severity: Literal["blocker", "warning"]
    code: str
    message: str


@dataclass(frozen=True)
class CompositionContext:
    source_job_id: str
    intake_digest: str
    discipline_id: str | None
    course_id: str | None
    chapter_id: str | None
    learning_content: tuple[str, ...]
    concept_candidates: tuple[str, ...]
    learner_evidence: tuple[str, ...]
    misconceptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    warnings: tuple[CompositionIssue, ...]
    proposed_target_path: str
    template: str
    current_note: str | None


@dataclass(frozen=True)
class CompositionResult:
    markdown: str
    target_path: str
    warnings: tuple[CompositionIssue, ...]


class NoteComposer(Protocol):
    def compose(self, context: CompositionContext) -> str: ...


class StubNoteComposer:
    """Deterministic local preview renderer; it has no external dependencies."""

    def compose(self, context: CompositionContext) -> str:
        sections = [context.template.rstrip(), "# Learning composition preview"]
        sections.append(
            "## Learning content\n" + "\n".join(f"- {x}" for x in context.learning_content)
        )
        if context.concept_candidates:
            sections.append(
                "## Concept candidates\n" + "\n".join(f"- {x}" for x in context.concept_candidates)
            )
        if context.learner_evidence:
            sections.append(
                "## Learner evidence\n" + "\n".join(f"- {x}" for x in context.learner_evidence)
            )
        if context.misconceptions:
            sections.append(
                "## Misconceptions\n" + "\n".join(f"- {x}" for x in context.misconceptions)
            )
        if context.unresolved_questions:
            sections.append(
                "## Unresolved questions\n"
                + "\n".join(f"- {x}" for x in context.unresolved_questions)
            )
        if context.current_note:
            sections.append("## Current note\n" + context.current_note.rstrip())
        return "\n\n".join(sections) + "\n"


def validate_target_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or ".." in path.parts
        or not value.startswith("MedLearn/")
        or path.suffix != ".md"
    ):
        raise ValueError("UNSAFE_COMPOSITION_TARGET_PATH")
    return path.as_posix()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_context(
    raw: bytes, *, template: str, current_note: str | None = None
) -> CompositionContext:
    """Parse an IntakeEnvelope, Handoff, or LearningSegment without strict proposal resolution."""
    try:
        envelope = IntakeEnvelope.model_validate_json(raw)
    except ValidationError:
        envelope = None
    if envelope is not None:
        draft = envelope.draft
        content = tuple(x.statement for x in draft.claim_candidates) + tuple(
            x.explanation_text for x in draft.generated_explanations
        )
        concepts = tuple(x.surface_text for x in draft.concept_mentions)
        evidence = tuple(x.rationale for x in draft.learner_evidence_candidates)
        misconceptions = tuple(x.observed_error_logic for x in draft.misconception_candidates)
        questions = tuple(x.statement for x in draft.claim_candidates if x.claim_type == "question")
        source_job_id = str(draft.context.session_id)
        discipline, course, chapter = (
            draft.context.discipline_id,
            draft.context.course_id,
            draft.context.chapter_id,
        )
    else:
        try:
            handoff = MedLearnHandoff.model_validate_json(raw)
        except ValidationError:
            try:
                handoff = LearningSegment.model_validate_json(raw).handoff
            except ValidationError as exc:
                raise ValueError("INVALID_COMPOSITION_INTAKE") from exc
        content = (
            tuple(x.statement for x in handoff.claims)
            + tuple(x.explanation_text for x in handoff.generated_explanations)
            + tuple(handoff.learning_goals)
        )
        concepts = tuple(x.name for x in handoff.concepts)
        evidence = tuple(x.rationale for x in handoff.learner_evidence)
        misconceptions = tuple(x.observed_error_logic for x in handoff.misconceptions)
        questions = tuple(x.statement for x in handoff.unresolved_questions)
        source_job_id = hashlib.sha256(handoff.session.title.encode("utf-8")).hexdigest()[:16]
        discipline, course, chapter = (
            handoff.session.discipline_id,
            handoff.session.course_id,
            handoff.session.chapter_id,
        )
    if not content:
        raise ValueError("NO_USABLE_LEARNING_CONTENT")
    warnings: list[CompositionIssue] = []
    if not concepts:
        warnings.append(
            CompositionIssue("warning", "UNRESOLVED_CONCEPT", "no stable concept target")
        )
    else:
        warnings.append(
            CompositionIssue(
                "warning", "CATALOG_UPDATE_REQUIRED", "concepts are not certified by composition"
            )
        )
    warnings.append(
        CompositionIssue(
            "warning", "SOURCE_MISSING", "source authority is not established by preview"
        )
    )
    if course is None:
        warnings.append(CompositionIssue("warning", "MISSING_COURSE_ID", "course_id is missing"))
    if chapter is None:
        warnings.append(CompositionIssue("warning", "MISSING_CHAPTER_ID", "chapter_id is missing"))
    warnings.append(
        CompositionIssue(
            "warning", "STRICT_PROPOSAL_NOT_APPROVED", "preview does not approve publication"
        )
    )
    target = validate_target_path(f"MedLearn/Inbox/{source_job_id}.md")
    return CompositionContext(
        source_job_id,
        _digest(raw),
        discipline,
        course,
        chapter,
        content,
        concepts,
        evidence,
        misconceptions,
        questions,
        tuple(warnings),
        target,
        template,
        current_note,
    )


def compose_preview(
    context: CompositionContext, composer: NoteComposer | None = None
) -> CompositionResult:
    markdown = (composer or StubNoteComposer()).compose(context)
    if not markdown.strip():
        raise ValueError("EMPTY_COMPOSER_OUTPUT")
    return CompositionResult(
        markdown, validate_target_path(context.proposed_target_path), context.warnings
    )
