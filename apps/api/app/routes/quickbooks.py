"""
Purpose: Expose authenticated QuickBooks Online integration routes for entity workspaces.
Scope: OAuth connect/callback, encrypted token persistence, disconnect/status management, and
QuickBooks chart-of-accounts synchronization into canonical COA sets.
Dependencies: FastAPI, local auth route helpers, entity/COA/integration repositories, QuickBooks
OAuth/client/sync services, and strict API contracts.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import get_auth_service
from apps.api.app.routes.close_runs import (
    _require_authenticated_browser_session,
    _resolve_trace_id,
    _to_entity_user,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from services.auth.service import AuthService
from services.coa.service import CoaRepository
from services.common.settings import AppSettings, get_settings
from services.contracts.quickbooks_models import (
    QuickBooksCoaSyncResponse,
    QuickBooksConnectionStatusResponse,
    QuickBooksConnectResponse,
    QuickBooksDisconnectResponse,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.integration import IntegrationConnectionStatus, IntegrationProvider
from services.db.repositories.entity_repo import EntityRepository
from services.db.repositories.integration_repo import IntegrationRepository
from services.entity.service import EntityService, EntityServiceError
from services.integrations.quickbooks.client import QuickBooksClient, QuickBooksClientError
from services.integrations.quickbooks.oauth import (
    QuickBooksOAuthError,
    QuickBooksReauthorizationRequiredError,
    get_quickbooks_oauth,
)
from services.integrations.quickbooks.sync_accounts import (
    QuickBooksSyncError,
    sync_chart_of_accounts,
)

router = APIRouter(tags=["integrations"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_entity_service(db_session: DatabaseSessionDependency) -> EntityService:
    """Construct the entity service from request-scoped persistence."""

    return EntityService(repository=EntityRepository(db_session=db_session))


def get_integration_repository(
    db_session: DatabaseSessionDependency,
) -> IntegrationRepository:
    """Construct the integration repository from request-scoped persistence."""

    return IntegrationRepository(db_session=db_session)


def get_coa_repository(db_session: DatabaseSessionDependency) -> CoaRepository:
    """Construct the COA repository from request-scoped persistence."""

    return CoaRepository(db_session=db_session)


EntityServiceDependency = Annotated[EntityService, Depends(get_entity_service)]
IntegrationRepositoryDependency = Annotated[
    IntegrationRepository,
    Depends(get_integration_repository),
]
CoaRepositoryDependency = Annotated[CoaRepository, Depends(get_coa_repository)]


@router.get(
    "/entities/{entity_id}/integrations/quickbooks/connect",
    response_model=QuickBooksConnectResponse,
    summary="Start QuickBooks Online OAuth connection",
)
def start_quickbooks_connection(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    return_url: Annotated[
        str | None,
        Query(description="Optional local UI URL to return to after the callback."),
    ] = None,
) -> QuickBooksConnectResponse:
    """Return the QuickBooks authorization URL for one accessible entity workspace."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_entity_access(
        entity_service=entity_service,
        entity_id=entity_id,
        user_id=session_result.user.id,
    )
    oauth = get_quickbooks_oauth(settings=settings)
    try:
        authorization = oauth.build_authorization_url(
            entity_id=entity_id,
            actor_user_id=session_result.user.id,
            return_url=return_url
            or f"/entities/{entity_id}/integrations?quickbooks=authorization_started",
        )
    except QuickBooksOAuthError as error:
        raise _build_quickbooks_http_exception(
            status_code=400,
            code="quickbooks_oauth_not_configured",
            message=str(error),
        ) from error

    return QuickBooksConnectResponse(authorization_url=authorization.authorization_url)


@router.get(
    "/integrations/quickbooks/callback",
    summary="Handle QuickBooks Online OAuth callback",
)
def complete_quickbooks_connection(
    code: str,
    state: str,
    realmId: str,  # noqa: N803 - Intuit sends camelCase realmId.
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    integration_repository: IntegrationRepositoryDependency,
) -> RedirectResponse:
    """Exchange callback code for tokens and persist encrypted QuickBooks credentials."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    oauth = get_quickbooks_oauth(settings=settings)
    try:
        callback_state = oauth.validate_state(state=state)
        if callback_state.actor_user_id != session_result.user.id:
            raise QuickBooksOAuthError("QuickBooks callback user did not match the signed state.")
        _require_entity_access(
            entity_service=entity_service,
            entity_id=callback_state.entity_id,
            user_id=session_result.user.id,
        )
        token_set = oauth.exchange_code_for_tokens(code=code, realm_id=realmId)
        encrypted_credentials = oauth.encrypt_token_set(
            entity_id=callback_state.entity_id,
            token_set=token_set,
        )
        integration_repository.upsert_connection(
            entity_id=callback_state.entity_id,
            provider=IntegrationProvider.QUICKBOOKS_ONLINE,
            status=IntegrationConnectionStatus.CONNECTED,
            encrypted_credentials=encrypted_credentials,
            external_realm_id=token_set.realm_id,
        )
        integration_repository.commit()
    except (QuickBooksOAuthError, EntityServiceError) as error:
        integration_repository.rollback()
        return RedirectResponse(
            url=_append_query_params(
                callback_state.return_url
                if "callback_state" in locals()
                else "/entities?quickbooks=connection_failed",
                {
                    "quickbooks": "connection_failed",
                    "message": str(error),
                },
            ),
            status_code=302,
        )
    except Exception:
        integration_repository.rollback()
        raise

    return RedirectResponse(
        url=_append_query_params(
            callback_state.return_url,
            {"quickbooks": "connected"},
        ),
        status_code=302,
    )


@router.get(
    "/entities/{entity_id}/integrations/quickbooks/status",
    response_model=QuickBooksConnectionStatusResponse,
    summary="Read QuickBooks Online connection status",
)
def read_quickbooks_status(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    integration_repository: IntegrationRepositoryDependency,
) -> QuickBooksConnectionStatusResponse:
    """Return sanitized connection status without exposing encrypted token material."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_entity_access(
        entity_service=entity_service,
        entity_id=entity_id,
        user_id=session_result.user.id,
    )
    connection = integration_repository.get_connection(
        entity_id=entity_id,
        provider=IntegrationProvider.QUICKBOOKS_ONLINE,
    )
    if connection is None:
        return QuickBooksConnectionStatusResponse(
            status="disconnected",
            recovery_action="Connect QuickBooks before syncing chart-of-accounts accounts.",
        )

    return QuickBooksConnectionStatusResponse(
        status=connection.status.value,
        external_realm_id=connection.external_realm_id,
        last_sync_at=connection.last_sync_at,
        recovery_action=_recovery_action_for_status(connection.status),
    )


@router.post(
    "/entities/{entity_id}/integrations/quickbooks/disconnect",
    response_model=QuickBooksDisconnectResponse,
    summary="Disconnect QuickBooks Online",
)
def disconnect_quickbooks(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    integration_repository: IntegrationRepositoryDependency,
) -> QuickBooksDisconnectResponse:
    """Revoke stored QuickBooks tokens and mark the connection as revoked."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_entity_access(
        entity_service=entity_service,
        entity_id=entity_id,
        user_id=session_result.user.id,
    )
    connection = integration_repository.get_connection(
        entity_id=entity_id,
        provider=IntegrationProvider.QUICKBOOKS_ONLINE,
    )
    if connection is None:
        return QuickBooksDisconnectResponse(
            status="disconnected",
            message="QuickBooks was not connected for this entity.",
        )

    oauth = get_quickbooks_oauth(settings=settings)
    try:
        token_set = oauth.decrypt_connection_tokens(connection=connection)
        oauth.revoke_token(token=token_set.refresh_token)
        oauth.revoke_token(token=token_set.access_token)
    except (QuickBooksOAuthError, QuickBooksReauthorizationRequiredError):
        # Revocation is best-effort after tokens expire; the local connection is still revoked.
        pass

    try:
        integration_repository.replace_encrypted_credentials(
            connection_id=connection.id,
            encrypted_credentials={},
            external_realm_id=connection.external_realm_id,
        )
        integration_repository.update_status(
            connection_id=connection.id,
            status=IntegrationConnectionStatus.REVOKED,
        )
        integration_repository.commit()
    except Exception:
        integration_repository.rollback()
        raise

    return QuickBooksDisconnectResponse(
        status=IntegrationConnectionStatus.REVOKED.value,
        message="QuickBooks has been disconnected for this entity.",
    )


@router.post(
    "/entities/{entity_id}/integrations/quickbooks/sync-coa",
    response_model=QuickBooksCoaSyncResponse,
    summary="Synchronize QuickBooks chart of accounts",
)
def sync_quickbooks_chart_of_accounts(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    integration_repository: IntegrationRepositoryDependency,
    coa_repository: CoaRepositoryDependency,
) -> QuickBooksCoaSyncResponse:
    """Import QuickBooks accounts into a versioned COA set without implementing direct posting."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_entity_access(
        entity_service=entity_service,
        entity_id=entity_id,
        user_id=session_result.user.id,
    )
    connection = integration_repository.get_connection(
        entity_id=entity_id,
        provider=IntegrationProvider.QUICKBOOKS_ONLINE,
    )
    if connection is None or connection.status is not IntegrationConnectionStatus.CONNECTED:
        raise _build_quickbooks_http_exception(
            status_code=409,
            code="quickbooks_reauthorization_required",
            message="QuickBooks is not connected. Reconnect QuickBooks before syncing accounts.",
        )

    oauth = get_quickbooks_oauth(settings=settings)
    quickbooks_client = QuickBooksClient(
        connection=connection,
        integration_repository=integration_repository,
        oauth=oauth,
        use_sandbox=settings.quickbooks.use_sandbox,
    )
    try:
        result = sync_chart_of_accounts(
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
            quickbooks_client=quickbooks_client,
            coa_repository=coa_repository,
            integration_repository=integration_repository,
            connection_id=connection.id,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except QuickBooksReauthorizationRequiredError as error:
        integration_repository.rollback()
        raise _build_quickbooks_http_exception(
            status_code=409,
            code="quickbooks_reauthorization_required",
            message=str(error),
        ) from error
    except (QuickBooksClientError, QuickBooksSyncError) as error:
        integration_repository.rollback()
        raise _build_quickbooks_http_exception(
            status_code=502,
            code="quickbooks_sync_failed",
            message=str(error),
        ) from error

    return QuickBooksCoaSyncResponse(
        account_count=result.account_count,
        activated=result.activated,
        coa_set_id=str(result.coa_set.id),
        message=f"Synchronized {result.account_count} QuickBooks accounts.",
        synced_at=result.synced_at,
        version_no=result.coa_set.version_no,
    )


def _require_entity_access(
    *,
    entity_service: EntityService,
    entity_id: UUID,
    user_id: UUID,
) -> None:
    """Validate that the authenticated user can manage the target entity integration."""

    try:
        entity_service.get_entity_workspace(user_id=user_id, entity_id=entity_id)
    except EntityServiceError as error:
        raise _build_quickbooks_http_exception(
            status_code=error.status_code,
            code=str(error.code),
            message=error.message,
        ) from error


def _recovery_action_for_status(status: IntegrationConnectionStatus) -> str | None:
    """Return an operator-facing recovery action for non-connected statuses."""

    if status is IntegrationConnectionStatus.CONNECTED:
        return None
    if status is IntegrationConnectionStatus.EXPIRED:
        return "Reconnect QuickBooks and retry chart-of-accounts sync."
    if status is IntegrationConnectionStatus.REVOKED:
        return "Connect QuickBooks again before syncing accounts."
    if status is IntegrationConnectionStatus.ERROR:
        return "Review the latest sync error, then reconnect QuickBooks if the issue persists."
    return None


def _append_query_params(url: str, params: dict[str, str]) -> str:
    """Append query parameters to an absolute or relative local return URL."""

    separator = "&" if "?" in url else "?"
    encoded = urlencode({key: value for key, value in params.items() if value})
    return f"{url}{separator}{encoded}" if encoded else url


def _build_quickbooks_http_exception(
    *,
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    """Convert integration-domain failures into the API's structured HTTP shape."""

    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


__all__ = ["router"]
