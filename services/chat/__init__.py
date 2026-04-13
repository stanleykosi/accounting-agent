"""
Purpose: Mark the canonical chat service package boundary.
Scope: Chat orchestration, grounding context resolution, and read-only
finance copilot analysis for the Accounting AI Agent.
Dependencies: Chat service and grounding modules.
"""

from services.chat.grounding import (
    ChatGroundingError,
    ChatGroundingErrorCode,
    ChatGroundingService,
    GroundingContextRecord,
)
from services.chat.service import ChatService, ChatServiceError, ChatServiceErrorCode

__all__ = [
    "ChatGroundingError",
    "ChatGroundingErrorCode",
    "ChatGroundingService",
    "ChatService",
    "ChatServiceError",
    "ChatServiceErrorCode",
    "GroundingContextRecord",
]
