"""Application services for the normalized education workflow.

Services orchestrate repositories and enforce cross-aggregate rules; they do
not own SQL and do not return ORM records. Each service raises DomainError
subtypes that the API layer maps to a stable ``{"error": {"code": ...}}`` body.
"""
