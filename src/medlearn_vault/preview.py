"""Generic deterministic medical preview planning and Markdown rendering."""

from typing import Literal

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.domain.ids import ConceptId, ScopedExternalId
from medlearn_vault.registry import resolve_alias
from medlearn_vault.terminology import (
    TerminologyPolicy,
    english_abbreviations,
    expand_registered_abbreviations,
    format_concept_label,
)


class PreviewBuildError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PreviewRequest(DomainModel):
    topic: str
    locale: Literal["zh-CN"] = "zh-CN"
    chapter_ids: tuple[ScopedExternalId, ...] = ()
    session_ids: tuple[ScopedExternalId, ...] = ()


class PreviewConceptLabel(DomainModel):
    concept_id: ConceptId
    surface_text: str | None = None


class PreviewClaim(DomainModel):
    statement: str
    concept_ids: tuple[ConceptId, ...]


class PreviewChapter(DomainModel):
    discipline_id: str
    title: str
    anchor_labels: tuple[PreviewConceptLabel, ...]
    focus_questions: tuple[str, ...]


class PreviewObservation(DomainModel):
    concept_labels: tuple[PreviewConceptLabel, ...]
    observed_error: str
    proposed_correction: str | None
    correction_claims: tuple[PreviewClaim, ...]


class PreviewPlan(DomainModel):
    topic: PreviewConceptLabel
    concepts: tuple[ConceptEntity, ...]
    mentions: tuple[PreviewConceptLabel, ...]
    claims: tuple[PreviewClaim, ...]
    chapters: tuple[PreviewChapter, ...]
    observations: tuple[PreviewObservation, ...]


def _resolve_topic(bundle: ContractBundle, topic: str) -> ConceptId:
    concepts = {item.concept_id: item for item in bundle.concepts}
    if topic in concepts:
        concept = concepts[topic]
        if concept.status == "active":
            return concept.concept_id
        if concept.status == "merged" and concept.merged_into in concepts:
            return concepts[concept.merged_into].concept_id
        if concept.status == "split_pending":
            raise PreviewBuildError("REVIEW_REQUIRED", f"topic requires review: {topic}")
        raise PreviewBuildError("TOPIC_NOT_FOUND", f"topic is not active: {topic}")
    resolution = resolve_alias(topic, bundle.concepts)
    if resolution.status in {"resolved", "redirected"} and resolution.resolved_concept_id:
        return resolution.resolved_concept_id
    if resolution.status == "ambiguous":
        raise PreviewBuildError("AMBIGUOUS_TOPIC", f"ambiguous topic: {topic}")
    if resolution.status == "review_required":
        raise PreviewBuildError("REVIEW_REQUIRED", f"topic requires review: {topic}")
    raise PreviewBuildError("TOPIC_NOT_FOUND", f"topic not found: {topic}")


def build_preview_plan(bundle: ContractBundle, request: PreviewRequest) -> PreviewPlan:
    errors = tuple(issue for issue in bundle.validate_integrity() if issue.severity == "error")
    if errors:
        raise PreviewBuildError("BUNDLE_INVALID", f"bundle has {len(errors)} integrity error(s)")
    topic_id = _resolve_topic(bundle, request.topic)
    claims = {item.claim_id: item for item in bundle.claims}

    chapters = tuple(
        chapter
        for chapter in bundle.chapters
        if topic_id in chapter.concept_ids
        and (not request.chapter_ids or chapter.chapter_id in request.chapter_ids)
    )
    captures = tuple(
        capture
        for capture in bundle.learning_captures
        if (not request.session_ids or capture.session_id in request.session_ids)
        and (
            any(topic_id in mention.candidate_concept_ids for mention in capture.concept_mentions)
            or any(topic_id in item.concept_ids for item in capture.misconception_observations)
        )
    )
    relevant_claim_ids = {
        claim.claim_id for claim in bundle.claims if topic_id in claim.concept_ids
    }
    relevant_claim_ids.update(
        claim_id
        for relation in bundle.relations
        if topic_id in {relation.source_concept_id, relation.target_concept_id}
        for claim_id in relation.supporting_claim_ids
    )
    relevant_claim_ids.update(
        claim_id
        for capture in captures
        for observation in capture.misconception_observations
        for claim_id in observation.correction_claim_ids
    )

    return PreviewPlan(
        topic=PreviewConceptLabel(
            concept_id=topic_id,
            surface_text=None if request.topic == topic_id else request.topic,
        ),
        concepts=tuple(bundle.concepts),
        mentions=tuple(
            PreviewConceptLabel(
                concept_id=mention.resolved_concept_id,
                surface_text=mention.surface_text,
            )
            for capture in captures
            for mention in capture.concept_mentions
            if mention.resolved_concept_id is not None
        ),
        claims=tuple(
            PreviewClaim(statement=claim.statement, concept_ids=claim.concept_ids)
            for claim in bundle.claims
            if claim.claim_id in relevant_claim_ids and claim.claim_status == "active"
        ),
        chapters=tuple(
            PreviewChapter(
                discipline_id=chapter.discipline_id,
                title=chapter.title,
                anchor_labels=tuple(
                    PreviewConceptLabel(concept_id=item) for item in chapter.anchor_concept_ids
                ),
                focus_questions=tuple(
                    question
                    for lens in bundle.discipline_lenses
                    if lens.discipline_id == chapter.discipline_id
                    and lens.concept_id in chapter.concept_ids
                    for question in lens.focus_questions
                ),
            )
            for chapter in sorted(chapters, key=lambda item: item.chapter_id)
        ),
        observations=tuple(
            PreviewObservation(
                concept_labels=tuple(
                    PreviewConceptLabel(concept_id=item) for item in observation.concept_ids
                ),
                observed_error=observation.observed_error_logic,
                proposed_correction=observation.proposed_correction,
                correction_claims=tuple(
                    PreviewClaim(
                        statement=claims[item].statement, concept_ids=claims[item].concept_ids
                    )
                    for item in observation.correction_claim_ids
                ),
            )
            for capture in captures
            for observation in capture.misconception_observations
        ),
    )


def render_markdown(plan: PreviewPlan) -> str:
    concepts = {item.concept_id: item for item in plan.concepts}
    policy = TerminologyPolicy()
    expanded: set[ConceptId] = set()

    def label(value: PreviewConceptLabel) -> str:
        concept = concepts[value.concept_id]
        abbreviations = english_abbreviations(concept)
        is_abbreviation = any(
            item.casefold() == (value.surface_text or "").casefold() for item in abbreviations
        ) or (value.surface_text is None and bool(abbreviations))
        local_policy = policy.model_copy(
            update={"expand_abbreviation_on_first_use": value.concept_id not in expanded}
        )
        result = format_concept_label(concept, surface_text=value.surface_text, policy=local_policy)
        if is_abbreviation:
            expanded.add(value.concept_id)
        return result

    title = label(plan.topic)
    lines = [f"# {title}跨学科学习预览", "", "## 概念识别", ""]
    lines.extend(f"- {label(item)}" for item in plan.mentions)
    lines.extend(["", "## 医学陈述", ""])
    for claim in plan.claims:
        text, expanded = expand_registered_abbreviations(
            claim.statement,
            concepts=tuple(concepts.values()),
            concept_ids=claim.concept_ids,
            already_expanded=expanded,
            policy=policy,
        )
        lines.append(f"- {text}")
    lines.extend(["", "## 学科章节", ""])
    for chapter in plan.chapters:
        lines.extend(
            [
                f"### {chapter.discipline_id} · {chapter.title}",
                "",
                f"章节锚点：{'、'.join(label(item) for item in chapter.anchor_labels)}",
            ]
        )
        if chapter.focus_questions:
            lines.append(f"关注问题：{'；'.join(chapter.focus_questions)}")
        lines.append("")
    lines.extend(["## 学习观察", ""])
    for observation in plan.observations:
        lines.append(f"- 涉及概念：{'、'.join(label(item) for item in observation.concept_labels)}")
        lines.append(f"  - 观察到的错误：{observation.observed_error}")
        if observation.proposed_correction:
            lines.append(f"  - 会话中的建议纠正：{observation.proposed_correction}")
        for claim in observation.correction_claims:
            text, expanded = expand_registered_abbreviations(
                claim.statement,
                concepts=tuple(concepts.values()),
                concept_ids=claim.concept_ids,
                already_expanded=expanded,
                policy=policy,
            )
            lines.append(f"  - 已验证陈述：{text}")
    return "\n".join(lines).rstrip() + "\n"
