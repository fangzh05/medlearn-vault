# Learning-chat source identity

`medlearn.learning_chat_source.v1` derives a learning-chat `source_id` from a
CaptureContext without using `source_id` itself.  Python and the Worker hash
the same bytes:

1. UTF-8 `medlearn.learning_chat_source.v1`.
2. Seven NUL-separated fields, in this order: `session_id`, `discipline_id`,
   nullable `course_id`, nullable `chapter_id`, `locale`,
   `session_started_at`, and `captured_at`.
3. A present field is `S` + its UTF-8 byte length in ASCII + `:` + its UTF-8
   bytes; a null field is `N`.
4. `source_id` is `source_` plus the first 32 lowercase SHA-256 hex digits.

Timestamps are carried exactly from the Handoff conversion and nullable course
and chapter fields remain distinct from strings.  The shared sanitized golden
is [apl-bootstrap-identity.json](../examples/intake/apl-bootstrap-identity.json).
