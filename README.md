# MedLearn Vault

MedLearn Vault is a local-first contract layer for canonical medical concepts,
cross-disciplinary chapter dossiers, source-backed claims, and learner evidence.

This repository currently implements **P0 only**: domain models, deterministic
identifiers, validation, schema export, a small CLI, tests, and CI. It performs no
Vault writes and contains no LLM, database, Obsidian, or document-ingestion integration.

```powershell
python -m pip install -e ".[dev]"
medlearn doctor
pytest
```

