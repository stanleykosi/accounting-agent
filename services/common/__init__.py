"""
Purpose: Expose shared backend infrastructure primitives used across API,
worker, and domain services.
Scope: Environment-backed settings, structured logging bootstrap,
deployment enums, domain enums, and low-level helper types.
Dependencies: services/common/settings.py, services/common/logging.py,
services/common/types.py, and services/common/enums.py.
"""

from services.common.enums import (
    ArtifactType,
    AutonomyMode,
    CANONICAL_WORKFLOW_PHASES,
    CloseRunPhaseStatus,
    CloseRunStatus,
    JobStatus,
    ReviewStatus,
    WorkflowPhase,
)
from services.common.logging import (
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)
from services.common.settings import AppSettings, get_settings, reset_settings_cache
from services.common.types import DeploymentEnvironment, StructuredLogFormat

__all__ = [
    "AppSettings",
    "ArtifactType",
    "AutonomyMode",
    "CANONICAL_WORKFLOW_PHASES",
    "CloseRunPhaseStatus",
    "CloseRunStatus",
    "DeploymentEnvironment",
    "JobStatus",
    "ReviewStatus",
    "StructuredLogFormat",
    "WorkflowPhase",
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "get_logger",
    "get_settings",
    "reset_settings_cache",
]
