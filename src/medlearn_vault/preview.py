"""Deterministic preview plan and Markdown renderer."""

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.registry import resolve_alias


class PreviewMention(DomainModel):
    surface_text: str
    concept_name: str


class PreviewChapter(DomainModel):
    discipline_id: str
    title: str
    anchor_names: tuple[str, ...]
    focus_questions: tuple[str, ...]


class PreviewObservation(DomainModel):
    concept_names: tuple[str, ...]
    observed_error: str
    proposed_correction: str | None
    correction_claims: tuple[str, ...]


class PreviewPlan(DomainModel):
    title: str
    mentions: tuple[PreviewMention, ...]
    claims: tuple[str, ...]
    chapters: tuple[PreviewChapter, ...]
    observations: tuple[PreviewObservation, ...]


def build_preview_plan(bundle: ContractBundle) -> PreviewPlan:
    issues = bundle.validate_integrity()
    if issues:
        raise ValueError(f"bundle has {len(issues)} integrity issue(s)")
    concepts = {item.concept_id: item for item in bundle.concepts}
    claims = {item.claim_id: item for item in bundle.claims}

    mentions: list[PreviewMention] = []
    for capture in bundle.learning_captures:
        for mention in capture.concept_mentions:
            resolution = resolve_alias(mention.surface_text, bundle.concepts)
            if resolution.resolved_concept_id is not None:
                mentions.append(
                    PreviewMention(
                        surface_text=mention.surface_text,
                        concept_name=concepts[resolution.resolved_concept_id].canonical_name,
                    )
                )

    chapters = tuple(
        PreviewChapter(
            discipline_id=chapter.discipline_id,
            title=chapter.title,
            anchor_names=tuple(
                concepts[item].canonical_name for item in chapter.anchor_concept_ids
            ),
            focus_questions=tuple(
                question
                for lens in bundle.discipline_lenses
                if lens.discipline_id == chapter.discipline_id
                and lens.concept_id in chapter.concept_ids
                for question in lens.focus_questions
            ),
        )
        for chapter in sorted(bundle.chapters, key=lambda item: item.chapter_id)
    )
    observations = tuple(
        PreviewObservation(
            concept_names=tuple(concepts[item].canonical_name for item in observation.concept_ids),
            observed_error=observation.observed_error_logic,
            proposed_correction=observation.proposed_correction,
            correction_claims=tuple(
                claims[item].statement for item in observation.correction_claim_ids
            ),
        )
        for capture in bundle.learning_captures
        for observation in capture.misconception_observations
    )
    return PreviewPlan(
        title="GERD 跨学科学习预览",
        mentions=tuple(mentions),
        claims=tuple(item.statement for item in bundle.claims if item.claim_status == "active"),
        chapters=chapters,
        observations=observations,
    )


def render_markdown(plan: PreviewPlan) -> str:
    lines = [f"# {plan.title}", "", "## 概念识别", ""]
    lines.extend(f"- {item.surface_text} → {item.concept_name}" for item in plan.mentions)
    lines.extend(["", "## 医学陈述", ""])
    lines.extend(f"- {statement}" for statement in plan.claims)
    lines.extend(["", "## 学科章节", ""])
    for chapter in plan.chapters:
        lines.extend(
            [
                f"### {chapter.discipline_id} · {chapter.title}",
                "",
                f"章节锚点：{'、'.join(chapter.anchor_names)}",
            ]
        )
        if chapter.focus_questions:
            lines.append(f"关注问题：{'；'.join(chapter.focus_questions)}")
        lines.append("")
    lines.extend(["## 学习观察", ""])
    for observation in plan.observations:
        lines.append(f"- 涉及概念：{'、'.join(observation.concept_names)}")
        lines.append(f"  - 观察到的错误：{observation.observed_error}")
        if observation.proposed_correction:
            lines.append(f"  - 会话中的建议纠正：{observation.proposed_correction}")
        lines.extend(f"  - 已验证陈述：{item}" for item in observation.correction_claims)
    return "\n".join(lines).rstrip() + "\n"
