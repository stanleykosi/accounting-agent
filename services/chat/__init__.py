"""
Purpose: Mark the canonical chat service package boundary.
Scope: Chat orchestration, grounding context resolution, action routing for
proposed edits, and finance copilot analysis for the Accounting AI Agent.
Dependencies: Chat service, grounding module, action router, and proposed changes.
"""

from services.chat.action_router import (
    ChatActionRouter,
    ChatActionRouterError,
    ChatActionRouterErrorCode,
)
from services.chat.grounding import (
    ChatGroundingError,
    ChatGroundingErrorCode,
    ChatGroundingService,
    GroundingContextRecord,
)
from services.chat.proposed_changes import (
    ProposedChangesError,
    ProposedChangesErrorCode,
    ProposedChangesService,
)
from services.chat.service import ChatService, ChatServiceError, ChatServiceErrorCode

__all__ = [
    "ChatActionRouter",
    "ChatActionRouterError",
    "ChatActionRouterErrorCode",
    "ChatGroundingError",
    "ChatGroundingErrorCode",
    "ChatGroundingService",
    "ChatService",
    "ChatServiceError",
    "ChatServiceErrorCode",
    "GroundingContextRecord",
    "ProposedChangesError",
    "ProposedChangesErrorCode",
    "ProposedChangesService",
]
