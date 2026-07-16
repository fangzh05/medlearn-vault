# Native PDF source extraction

`medlearn sources extract-pdf` reads only the existing native text layer of private PDFs. OCR is intentionally excluded: this command never renders pages or interprets images. Source PDFs are immutable; generated inspection files are replaceable and must never be committed.

Install the optional backend with `pip install "medlearn-vault[pdf]"`. For one file:

```powershell
medlearn sources extract-pdf --input "<source-root>\raw\book.pdf" --output-root "<source-root>\generated"
```

For a source directory, matching is recursive and case-insensitive:

```powershell
medlearn sources extract-pdf --input "<source-root>\raw" --output-root "<source-root>\generated" --json
```

Each PDF produces `pages.jsonl`, `fulltext.txt`, and `report.json` beneath the output root while preserving its relative source directory. `pages.jsonl` has exactly one UTF-8/LF JSON record for each 1-based PDF page. `fulltext.txt` has explicit page markers. The extractor deliberately does not remove headers or footers, correct text, chunk content, index it, or certify medical accuracy.

Warnings (`EMPTY_PAGES_PRESENT`, `LOW_TEXT_PAGES_PRESENT`, `POSSIBLE_IMAGE_ONLY_PDF`, `POSSIBLE_BROKEN_TEXT_LAYER`, and `REPLACEMENT_CHARACTERS_PRESENT`) do not fail a readable PDF. Inspect `pages.jsonl` and `fulltext.txt` locally before trusting text quality. A directory batch continues after failures and returns nonzero only if any PDF fails; an empty directory is rejected.
