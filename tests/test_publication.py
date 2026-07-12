import hashlib

from test_workflow import NOW, ROOT, MemoryStore, seed_proposal

from medlearn_vault.workflow import ApprovalOrchestrator, PublicationPlanOrchestrator


def test_publication_plan_is_canonical_create_only_and_reusable() -> None:
    store = MemoryStore()
    proposal_id, proposal_digest, base_digest, _ = seed_proposal(store)
    approval = ApprovalOrchestrator(store).run(
        proposal_id, proposal_digest, base_digest, now=NOW
    )
    approval_body = store.objects[f"v1/approvals/{approval.approval_id}.json"].body
    result = PublicationPlanOrchestrator(store, ROOT).run(
        approval.approval_id,
        "sha256:" + hashlib.sha256(approval_body).hexdigest(),
        "job-approval-source",
        proposal_id,
        proposal_digest,
        base_digest,
        bundle_path="examples/copd",
    )
    assert result.reused is False
    assert store.creates[-1] == f"v1/publication-plans/{result.publication_plan_id}.json"
    rerun = PublicationPlanOrchestrator(store, ROOT).run(
        approval.approval_id,
        "sha256:" + hashlib.sha256(approval_body).hexdigest(),
        "job-approval-source",
        proposal_id,
        proposal_digest,
        base_digest,
        bundle_path="examples/copd",
    )
    assert rerun == result.model_copy(update={"reused": True})
