# ADR-003: Separate medical authority from course relevance

Status: Accepted

Medical authority is an intrinsic source-quality score; course relevance is a mapping from
course IDs to relevance scores. They are independent because a highly authoritative source
may not match an examination scope, while course material may be relevant but outdated.

