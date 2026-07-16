# Source normalization

`medlearn sources normalize` consumes immutable extracted `pages.jsonl` and `report.json` files and writes replaceable local normalized outputs. It is deterministic, local-only, and preserves one 1-based record per PDF page.

It normalizes LF line endings, BOM/NUL, edge whitespace and excessive blank lines, applies explicit local exclusions, and removes only conservative repeated edge headers, footers, and edge page numbers (at least 60% and five included pages). It does not perform OCR, medical correction, paragraph joining, two-column reconstruction, chunking, indexing, retrieval, or LLM work.

```powershell
medlearn sources normalize --input-root "<source-root>\generated" --output-root "<source-root>\normalized" --exclusions "<source-root>\metadata\page-exclusions.json" --json
```

The exclusion manifest is local UTF-8 JSON with version `1`, safe relative source paths, 1-based pages, and one of `BROKEN_TEXT_LAYER`, `IMAGE_ONLY_PAGE`, `MANUAL_QUALITY_EXCLUSION`, or `EXTRACTION_ORDER_UNUSABLE`. Private input, exclusion manifests, and normalized text must not be committed.
