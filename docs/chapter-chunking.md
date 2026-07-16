# Deterministic chapter-aware chunking

`medlearn sources chunk` consumes only a normalization directory containing
`normalized-pages.jsonl` and `normalization-report.json`. Normalization output is
immutable; PDF extraction, OCR, correction, indexing, retrieval, embeddings,
LLMs, Workers, R2/Vault, manifests, and sync are intentionally out of scope.

Use `--input-root <source-root>` and a separate `--output-root <source-root>`.
The command emits `sections.jsonl`, `chunks.jsonl`, and `chunking-report.json`
per source. Default character limits are target 1600, maximum 2200, overlap 160.

The input contract validates UTF-8, page sequence, source identity, safe metadata,
normalization version, status accounting, and an advertised normalized-page digest.
Expected failures use stable `CHUNKING_*` codes. Outputs are committed through a
complete directory transaction and identical output is idempotent; changed output
requires `--force`.

Heading detection is conservative and deterministic: explicit Chinese/English
part, chapter, and section markers plus bounded numbered forms only at page edges
or blank-line boundaries. Figure/table/reference/list-like lines and page numbers
are rejected. Possible contents pages retain text but cannot create boundaries.
Every source has a synthetic root; level jumps and duplicate adjacent headings are
reported as warnings.

Chunks preserve normalized text and page-local half-open provenance spans.
Excluded pages have no chunks and form hard boundaries. Empty pages remain in
accounting. Reports contain counts and hashes only; CLI summaries do not expose
source text or titles.
