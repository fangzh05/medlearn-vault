# Local source search

`medlearn sources index` reads local chunking output directories containing
`sections.jsonl`, `chunks.jsonl`, and `chunking-report.json`, validates their
key identity and integrity fields, and atomically builds one SQLite database.
The database stores metadata, source identities, and chunks with ordinary
indexes only. It contains no absolute paths, timestamps, or machine details.

`medlearn sources search QUERY --index PATH` performs deterministic lexical
matching: every whitespace-delimited term must occur in the chunk text, section
titles, or source path. Exact chunk phrases score highest; chunk terms, section
titles, and source paths have fixed documented weights. Results are ordered by
score, casefolded source path, page, chunk index, and chunk ID. `--source`
filters by a case-insensitive source-path substring. JSON output includes full
local chunk text for this explicit local action.

Re-run the same input safely: matching `input_digest` metadata is idempotent.
Use `--force` to atomically replace a changed index. The index and source text
stay local and must not be committed or synchronized.

Current limitations: no semantic search, fuzzy matching, Chinese word
segmentation, BM25, embeddings, reranking, FTS, or query DSL.
