"""
Purpose: Define the shared contract package boundary for API and domain schema modules.
Scope: Re-export the seed API contract models consumed by the FastAPI application
and SDK generation.
Dependencies: services/contracts/api_models.py and future contract modules added in later steps.
"""

from services.contracts.api_models import ApiContractMetadata, ApiHealthStatus, ApiRouteDescriptor

__all__ = ["ApiContractMetadata", "ApiHealthStatus", "ApiRouteDescriptor"]
