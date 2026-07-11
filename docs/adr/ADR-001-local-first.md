# ADR-001: Local-first architecture

Status: Accepted

The canonical store will be the user's local Vault. P0 therefore defines pure contracts
without network, database, or Obsidian dependencies. Later adapters must expose previews
and transactional writes; the domain layer must remain usable offline.

