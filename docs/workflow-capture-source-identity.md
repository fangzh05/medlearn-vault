# Learning-chat source identity

`medlearn.learning_chat_source.v2` derives a stable, non-authoritative
learning-chat `source_id` from a CaptureContext without using `source_id`
itself.  Python and the Worker hash the same bytes:

1. UTF-8 `medlearn.learning_chat_source.v2`.
2. Four NUL-separated fields, in this order: `discipline_id`, nullable
   `course_id`, nullable `chapter_id`, and `locale`.
3. A present field is `S` + its UTF-8 byte length in ASCII + `:` + its UTF-8
   bytes; a null field is `N`.
4. `source_id` is `source_` plus the first 32 lowercase SHA-256 hex digits.

`session_id`, `session_started_at`, and `captured_at` are provenance of a
specific CaptureContext and evidence messages.  They are intentionally excluded
from source identity so repeated ChatGPT Work captures of the same
discipline/course/chapter do not create a new source catalog candidate merely
because they were captured at a different time.

The source remains `source_type=learning_chat` and `authority=0`; chat claims
must not be promoted into authoritative medical facts by source bootstrap.
Medical concepts still require explicit catalog review, and the converter never
auto-creates medical concepts.

Pre-v3 handoff intakes used `medlearn.learning_chat_source.v1`, which included
`session_id`, `session_started_at`, and `captured_at`.  Those stored intakes,
jobs, proposals, and source IDs remain immutable.  New ChatGPT Work submissions
use the `medlearn.handoff_to_intake.v4` idempotency namespace and the v2 source
identity.  The shared sanitized golden is
[apl-bootstrap-identity.json](../examples/intake/apl-bootstrap-identity.json).
