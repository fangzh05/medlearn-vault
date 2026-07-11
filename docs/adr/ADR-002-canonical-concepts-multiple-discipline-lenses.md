# ADR-002: Canonical concepts with multiple discipline lenses

Status: Accepted

A medical concept has one canonical identity and may carry many `DisciplineLens` records.
Course chapters reference that shared identity instead of copying the concept. A lens must
reference its enclosing concept, preventing accidental cross-concept attachment.

