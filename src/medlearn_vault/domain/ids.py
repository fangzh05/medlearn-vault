from typing import Annotated

from pydantic import StringConstraints

ConceptId = Annotated[str, StringConstraints(pattern=r"^concept_[a-f0-9]{32}$")]
ClaimId = Annotated[str, StringConstraints(pattern=r"^claim_[a-f0-9]{32}$")]
SourceId = Annotated[str, StringConstraints(pattern=r"^source_[a-f0-9]{32}$")]
RelationId = Annotated[str, StringConstraints(pattern=r"^relation_[a-f0-9]{32}$")]
LensId = Annotated[str, StringConstraints(pattern=r"^lens_[a-f0-9]{32}$")]
UnitId = Annotated[str, StringConstraints(pattern=r"^unit_[a-f0-9]{32}$")]
ScopedExternalId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:-]{2,127}$")]
