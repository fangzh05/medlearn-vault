"""Local, deterministic note-composition preview from persisted intake data."""

from __future__ import annotations

import hashlib
import json
import re
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
    source_job_id: str | None
    source_record_id: str
    intake_digest: str
    discipline_id: str | None
    course_id: str | None
    chapter_id: str | None
    learning_content: tuple[str, ...]
    concept_candidates: tuple[str, ...]
    learner_evidence: tuple[str, ...]
    misconceptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    isolated_items: tuple[str, ...]
    warnings: tuple[CompositionIssue, ...]
    proposed_target_path: str
    template: str
    current_note: str | None


@dataclass(frozen=True)
class CompositionResult:
    markdown: str
    target_path: str
    warnings: tuple[CompositionIssue, ...]
    isolated_items: tuple[str, ...]


@dataclass(frozen=True)
class CompositionValidationResult:
    status: Literal["accepted", "accepted_with_warnings", "rejected"]
    blockers: tuple[CompositionIssue, ...] = ()
    warnings: tuple[CompositionIssue, ...] = ()
    isolated_items: tuple[str, ...] = ()


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
        or "\0" in value
        or ".." in path.parts
        or not value.startswith("MedLearn/")
        or path.suffix != ".md"
    ):
        raise ValueError("UNSAFE_COMPOSITION_TARGET_PATH")
    return path.as_posix()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _source_job_id(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None:
        raise ValueError("INVALID_SOURCE_JOB_ID")
    return value


def build_context(
    raw: bytes,
    *,
    template: str,
    current_note: str | None = None,
    source_job_id: str | None = None,
    expected_intake_digest: str | None = None,
) -> CompositionContext:
    """Parse an IntakeEnvelope, Handoff, or LearningSegment without strict proposal resolution."""
    digest = _digest(raw)
    if expected_intake_digest is not None and expected_intake_digest != digest:
        raise ValueError("COMPOSITION_INPUT_DIGEST_MISMATCH")
    isolated: tuple[str, ...] = ()
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
        discipline, course, chapter = (
            draft.context.discipline_id,
            draft.context.course_id,
            draft.context.chapter_id,
        )
    else:
        tolerant = False
        try:
            handoff = MedLearnHandoff.model_validate_json(raw)
        except ValidationError:
            try:
                handoff = LearningSegment.model_validate_json(raw).handoff
            except ValidationError:
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from exc
                if (
                    not isinstance(payload, dict)
                    or "session" not in payload
                    or "evidence_messages" not in payload
                ):
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                if not isinstance(payload["evidence_messages"], list):
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                roles = {
                    x.get("local_id"): x.get("role")
                    for x in payload.get("evidence_messages", [])
                    if isinstance(x, dict)
                }

                def conflict(item: object, key: str, required: str | None = None) -> bool:
                    refs = item.get(key, []) if isinstance(item, dict) else []
                    seen = {roles.get(x) for x in refs}
                    return (
                        len(seen) != 1
                        or None in seen
                        or (required is not None and seen != {required})
                    )

                for collection, key, required in (
                    ("claims", "evidence_local_ids", None),
                    ("learner_evidence", "evidence_local_ids", "user"),
                ):
                    if not isinstance(payload.get(collection, []), list):
                        raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                    kept = []
                    for index, item in enumerate(payload[collection]):
                        if not isinstance(item, dict):
                            raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                        if conflict(item, key, required):
                            isolated += (f"{collection}[{index}]:EVIDENCE_ROLE_CONFLICT",)
                        else:
                            kept.append(item)
                    payload[collection] = kept
                if not isolated:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                try:
                    handoff = MedLearnHandoff.model_validate(payload)
                except ValidationError as exc:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from exc
                tolerant = False
        if not tolerant:
            assert handoff is not None
            content = (
                tuple(x.statement for x in handoff.claims)
                + tuple(x.explanation_text for x in handoff.generated_explanations)
                + tuple(handoff.learning_goals)
            )
            concepts = tuple(x.name for x in handoff.concepts)
            evidence = tuple(x.rationale for x in handoff.learner_evidence)
            misconceptions = tuple(x.observed_error_logic for x in handoff.misconceptions)
            questions = tuple(x.statement for x in handoff.unresolved_questions)
            discipline, course, chapter = (
                handoff.session.discipline_id,
                handoff.session.course_id,
                handoff.session.chapter_id,
            )
    usable_content = bool(content or evidence or misconceptions or questions)
    if not usable_content:
        raise ValueError("NO_USABLE_LEARNING_CONTENT")
    warnings: list[CompositionIssue] = []
    if isolated:
        warnings.append(
            CompositionIssue("warning", "EVIDENCE_ROLE_CONFLICT", "conflicted items isolated")
        )
    if not concepts:
        warnings.append(
            CompositionIssue("warning", "UNRESOLVED_CONCEPT", "no stable concept target")
        )
    if course is None:
        warnings.append(CompositionIssue("warning", "MISSING_COURSE_ID", "course_id is missing"))
    if chapter is None:
        warnings.append(CompositionIssue("warning", "MISSING_CHAPTER_ID", "chapter_id is missing"))
    job_id = _source_job_id(source_job_id)
    source_record_id = "preview_" + digest[7:23]
    target = validate_target_path(f"MedLearn/Inbox/{job_id or source_record_id}.md")
    return CompositionContext(
        job_id,
        source_record_id,
        digest,
        discipline,
        course,
        chapter,
        content,
        concepts,
        evidence,
        misconceptions,
        questions,
        isolated,
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
        markdown,
        validate_target_path(context.proposed_target_path),
        context.warnings,
        context.isolated_items,
    )


def validate_composition(context: CompositionContext) -> CompositionValidationResult:
    return CompositionValidationResult(
        "accepted_with_warnings" if context.warnings else "accepted",
        warnings=context.warnings,
        isolated_items=context.isolated_items,
    )
