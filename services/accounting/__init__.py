"""
Purpose: Mark the canonical deterministic accounting service package.
Scope: GL coding rules, preprocessing, policy gates, dimension helpers, journal drafting,
and recommendation approval routing used before and after model-backed recommendation workflows.
Dependencies: Accounting modules in this package and shared domain enums.
"""

from services.accounting.journal_drafts import (
    JournalDraftError,
    JournalDraftSpec,
    JournalLineSpec,
    build_journal_draft_from_recommendation,
    build_journal_draft_input,
    generate_journal_number,
)
from services.accounting.recommendation_apply import (
    ActorContext,
    JournalActionResult,
    RecommendationApplyError,
    RecommendationApplyResult,
    RecommendationApplyService,
)

__all__ = [
    "ActorContext",
    "JournalActionResult",
    "JournalDraftError",
    "JournalDraftSpec",
    "JournalLineSpec",
    "RecommendationApplyError",
    "RecommendationApplyResult",
    "RecommendationApplyService",
    "build_journal_draft_from_recommendation",
    "build_journal_draft_input",
    "generate_journal_number",
]
