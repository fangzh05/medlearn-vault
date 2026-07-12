# ADR-005: approved Vault publication roadmap

## Status

Accepted for planning; no publisher is implemented by this decision.

## Decision

The publication path will remain separated from the control plane and proceed in this order:

```text
Approval
-> Proposal revalidation
-> LearningCapture materialization
-> deterministic VaultPublicationPlan
-> append-only Vault writer
-> medlearn-commit.yml
-> encrypted R2 publication
-> Remotely Save synchronization
```

`medlearn-control` remains the workflow control plane. `medlearn-vault` becomes the approved
publication and Vault plane. Cloudflare R2 plus Remotely Save is the only cross-device sync layer.
No D1, VPS deployment, custom Obsidian sync plugin, or iCloud sharing of the production Vault is
part of this architecture.

## Consequences

This change adds neither a publisher nor `medlearn-commit.yml`. Future commit work must revalidate
the immutable approval and Proposal before every write, use an append-only writer, and keep
publication credentials separate from control credentials.
