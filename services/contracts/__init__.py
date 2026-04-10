"""
Purpose: Define the shared contract package boundary for API and domain schema modules.
Scope: Re-export the seed API contract models consumed by the FastAPI application
and SDK generation.
Dependencies: services/contracts/api_models.py, services/contracts/domain_models.py,
and future contract modules added in later steps.
"""

from services.contracts.api_models import (
    ApiContractMetadata,
    ApiHealthStatus,
    ApiRouteDescriptor,
)
from services.contracts.close_run_models import (
    CloseRunDecisionRequest,
    CloseRunListResponse,
    CloseRunReopenResponse,
    CloseRunSummary,
    CloseRunTransitionResponse,
    CreateCloseRunRequest,
    TransitionCloseRunRequest,
)
from services.contracts.domain_models import (
    DEFAULT_DOMAIN_LANGUAGE_CATALOG,
    CloseRunPhaseState,
    CloseRunWorkflowState,
    DomainLanguageCatalog,
    DomainValueDefinition,
    WorkflowPhaseDefinition,
    build_domain_language_catalog,
)

__all__ = [
    "DEFAULT_DOMAIN_LANGUAGE_CATALOG",
    "ApiContractMetadata",
    "ApiHealthStatus",
    "ApiRouteDescriptor",
    "CloseRunDecisionRequest",
    "CloseRunListResponse",
    "CloseRunPhaseState",
    "CloseRunReopenResponse",
    "CloseRunSummary",
    "CloseRunTransitionResponse",
    "CloseRunWorkflowState",
    "CreateCloseRunRequest",
    "DomainLanguageCatalog",
    "DomainValueDefinition",
    "TransitionCloseRunRequest",
    "WorkflowPhaseDefinition",
    "build_domain_language_catalog",
]
