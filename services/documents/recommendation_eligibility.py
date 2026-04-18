"""
Purpose: Define the canonical document types that should enter GL-coding recommendation flows.
Scope: Shared eligibility rules for worker auto-queueing, API/manual recommendation requests,
and close-run processing gate calculations.
Dependencies: Canonical document type enum only.
"""

from __future__ import annotations

from services.common.enums import DocumentType

GL_CODING_RECOMMENDATION_ELIGIBLE_DOCUMENT_TYPES = frozenset(
    {
        DocumentType.INVOICE,
        DocumentType.PAYSLIP,
        DocumentType.RECEIPT,
    }
)
GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES = tuple(
    document_type.value
    for document_type in GL_CODING_RECOMMENDATION_ELIGIBLE_DOCUMENT_TYPES
)


def is_gl_coding_recommendation_eligible(
    document_type: DocumentType | str | None,
) -> bool:
    """Return whether one document type should generate GL-coding recommendations."""

    if isinstance(document_type, DocumentType):
        resolved_document_type = document_type
    elif isinstance(document_type, str):
        try:
            resolved_document_type = DocumentType(document_type)
        except ValueError:
            return False
    else:
        return False

    return resolved_document_type in GL_CODING_RECOMMENDATION_ELIGIBLE_DOCUMENT_TYPES


__all__ = [
    "GL_CODING_RECOMMENDATION_ELIGIBLE_DOCUMENT_TYPES",
    "GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES",
    "is_gl_coding_recommendation_eligible",
]
