"""
Purpose: Re-export the model gateway and prompt registry surface for downstream consumers.
Scope: Bounded LLM access, prompt versioning, retries, and typed response validation.
Dependencies: services/model_gateway/client.py, services/model_gateway/prompts.py.
"""

from services.model_gateway.client import (
    ModelGateway,
    ModelGatewayConfig,
    ModelGatewayError,
    ModelGatewayRateLimitError,
    ModelGatewayToolCall,
    ModelResponseValidationError,
    get_gateway,
)
from services.model_gateway.prompts import (
    AMBIGUOUS_MAPPING_RANKING_PROMPT,
    DOCUMENT_CLASSIFICATION_PROMPT,
    GL_CODING_EXPLANATION_PROMPT,
    JOURNAL_NARRATIVE_PROMPT,
    PromptRegistryError,
    PromptTemplate,
    get_prompt_template,
    list_prompt_templates,
)

__all__ = [
    "AMBIGUOUS_MAPPING_RANKING_PROMPT",
    "DOCUMENT_CLASSIFICATION_PROMPT",
    "GL_CODING_EXPLANATION_PROMPT",
    "JOURNAL_NARRATIVE_PROMPT",
    "ModelGateway",
    "ModelGatewayConfig",
    "ModelGatewayError",
    "ModelGatewayRateLimitError",
    "ModelGatewayToolCall",
    "ModelResponseValidationError",
    "PromptRegistryError",
    "PromptTemplate",
    "get_gateway",
    "get_prompt_template",
    "list_prompt_templates",
]
