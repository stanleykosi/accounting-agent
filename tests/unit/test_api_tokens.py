"""
Purpose: Verify the canonical personal access token service behavior for CLI authentication.
Scope: Pure unit coverage over PAT issuance, scope enforcement, expiration, revocation,
and bearer-auth tracking using an in-memory repository double.
Dependencies: PAT service modules plus the shared password hasher and auth repository records.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from services.auth.api_tokens import (
    ApiTokenErrorCode,
    ApiTokenScope,
    ApiTokenService,
    ApiTokenServiceError,
)
from services.auth.passwords import PasswordHasher
from services.common.types import utc_now
from services.db.models.auth import UserStatus
from services.db.repositories.auth_repo import (
    ApiTokenRecord,
    ApiTokenWithUserRecord,
    AuthUserRecord,
)


def test_login_with_password_issues_hashed_api_token() -> None:
    """Ensure a successful CLI login returns a raw token while only its hash is persisted."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)

    result = service.login_with_password(
        email=user.email,
        password="CorrectHorse12",
        name="controller-laptop",
    )

    persisted_token = repository.api_tokens[result.api_token.id]
    assert result.plain_text_token.startswith("aat_")
    assert persisted_token.token_hash != result.plain_text_token
    assert persisted_token.user_id == user.id
    assert persisted_token.scope == ("cli:access",)
    assert repository.commit_calls == 1


def test_authenticate_token_updates_last_used_and_allows_required_scope() -> None:
    """Ensure successful bearer auth touches the PAT's last-used timestamp."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)
    issued = service.create_token_for_user(user_id=user.id, name="desktop-cli")

    observed_at = issued.api_token.created_at + timedelta(minutes=5)
    authenticated = service.authenticate_token(
        token=issued.plain_text_token,
        required_scopes=(ApiTokenScope.CLI_ACCESS,),
        now=observed_at,
    )

    assert authenticated.user.id == user.id
    assert authenticated.api_token.last_used_at == observed_at
    assert repository.commit_calls == 2


def test_authenticate_token_rejects_missing_scope() -> None:
    """Ensure bearer auth fails fast when the presented PAT lacks the required scope."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)
    issued = service.create_token_for_user(user_id=user.id, name="desktop-cli")
    repository.api_tokens[issued.api_token.id] = replace(issued.api_token, scope=())

    with pytest.raises(ApiTokenServiceError) as error:
        service.authenticate_token(
            token=issued.plain_text_token,
            required_scopes=(ApiTokenScope.CLI_ACCESS,),
        )

    assert error.value.status_code == 403
    assert error.value.code is ApiTokenErrorCode.INSUFFICIENT_SCOPE


def test_authenticate_token_rejects_revoked_tokens() -> None:
    """Ensure revoked PATs no longer authenticate future CLI requests."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)
    issued = service.create_token_for_user(user_id=user.id, name="desktop-cli")
    service.revoke_token_for_user(user_id=user.id, token_id=issued.api_token.id)

    with pytest.raises(ApiTokenServiceError) as error:
        service.authenticate_token(token=issued.plain_text_token)

    assert error.value.status_code == 401
    assert error.value.code is ApiTokenErrorCode.TOKEN_REVOKED


def test_authenticate_token_rejects_expired_tokens() -> None:
    """Ensure expired PATs fail fast and instruct the caller to create a replacement."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)
    issued = service.create_token_for_user(
        user_id=user.id,
        name="desktop-cli",
        expires_in_days=1,
    )

    with pytest.raises(ApiTokenServiceError) as error:
        service.authenticate_token(
            token=issued.plain_text_token,
            now=issued.api_token.expires_at + timedelta(seconds=1),
        )

    assert error.value.status_code == 401
    assert error.value.code is ApiTokenErrorCode.TOKEN_EXPIRED


def test_revoke_authenticated_token_revokes_the_presented_pat() -> None:
    """Ensure CLI logout can revoke the currently stored PAT without an extra lookup step."""

    repository = InMemoryApiTokenRepository()
    user = repository.seed_user(email="finance@example.com", password="CorrectHorse12")
    service = build_api_token_service(repository=repository)
    issued = service.create_token_for_user(user_id=user.id, name="desktop-cli")

    revoked = service.revoke_authenticated_token(
        token=issued.plain_text_token,
        required_scopes=(ApiTokenScope.CLI_ACCESS,),
    )

    assert revoked.id == issued.api_token.id
    assert revoked.revoked_at is not None


def build_api_token_service(*, repository: InMemoryApiTokenRepository) -> ApiTokenService:
    """Construct the PAT service with an in-memory repository for pure unit coverage."""

    return ApiTokenService(
        repository=repository,
        password_hasher=PasswordHasher(),
        token_signing_secret="test-token-signing-secret",
    )


class InMemoryApiTokenRepository:
    """Provide the minimal repository surface needed by the PAT service for unit tests."""

    def __init__(self) -> None:
        """Initialize in-memory user and PAT stores that mimic repository behavior."""

        self.users: dict[UUID, AuthUserRecord] = {}
        self.api_tokens: dict[UUID, ApiTokenRecord] = {}
        self.commit_calls = 0

    def seed_user(self, *, email: str, password: str) -> AuthUserRecord:
        """Create and store one active user record with a hashed password for CLI login tests."""

        user = AuthUserRecord(
            id=uuid4(),
            email=email,
            password_hash=PasswordHasher().hash_password(password),
            full_name="Finance Lead",
            status=UserStatus.ACTIVE,
            last_login_at=None,
        )
        self.users[user.id] = user
        return user

    def get_user_by_email(self, *, email: str) -> AuthUserRecord | None:
        """Return one user by canonical email, matching the production repository contract."""

        return next((user for user in self.users.values() if user.email == email), None)

    def get_user_by_id(self, *, user_id: UUID) -> AuthUserRecord | None:
        """Return one user by UUID, matching the production repository contract."""

        return self.users.get(user_id)

    def update_last_login(self, *, user_id: UUID, logged_in_at: datetime) -> AuthUserRecord:
        """Persist the most recent successful credential-login timestamp for one user."""

        updated_user = replace(self.users[user_id], last_login_at=logged_in_at)
        self.users[user_id] = updated_user
        return updated_user

    def create_api_token(
        self,
        *,
        user_id: UUID,
        name: str,
        token_hash: str,
        scope: tuple[str, ...],
        expires_at: datetime | None,
    ) -> ApiTokenRecord:
        """Store one new PAT record associated with the provided user."""

        now = utc_now()
        api_token = ApiTokenRecord(
            id=uuid4(),
            user_id=user_id,
            name=name,
            token_hash=token_hash,
            scope=scope,
            created_at=now,
            updated_at=now,
            last_used_at=None,
            revoked_at=None,
            expires_at=expires_at,
        )
        self.api_tokens[api_token.id] = api_token
        return api_token

    def list_api_tokens_for_user(self, *, user_id: UUID) -> tuple[ApiTokenRecord, ...]:
        """Return the current user's PATs in deterministic newest-first order."""

        tokens = [token for token in self.api_tokens.values() if token.user_id == user_id]
        return tuple(sorted(tokens, key=lambda token: token.created_at, reverse=True))

    def get_api_token_by_id_for_user(
        self,
        *,
        token_id: UUID,
        user_id: UUID,
    ) -> ApiTokenRecord | None:
        """Return one PAT by UUID when it belongs to the provided user."""

        api_token = self.api_tokens.get(token_id)
        if api_token is None or api_token.user_id != user_id:
            return None
        return api_token

    def get_api_token_with_user_by_hash(
        self,
        *,
        token_hash: str,
    ) -> ApiTokenWithUserRecord | None:
        """Return a joined PAT and user record when the token hash is known."""

        api_token = next(
            (
                candidate
                for candidate in self.api_tokens.values()
                if candidate.token_hash == token_hash
            ),
            None,
        )
        if api_token is None:
            return None

        return ApiTokenWithUserRecord(api_token=api_token, user=self.users[api_token.user_id])

    def update_api_token_last_used(
        self,
        *,
        token_id: UUID,
        last_used_at: datetime,
    ) -> ApiTokenRecord:
        """Update the last-used timestamp for one existing PAT."""

        updated_token = replace(
            self.api_tokens[token_id],
            last_used_at=last_used_at,
            updated_at=last_used_at,
        )
        self.api_tokens[token_id] = updated_token
        return updated_token

    def revoke_api_token(self, *, token_id: UUID, revoked_at: datetime) -> ApiTokenRecord:
        """Mark one PAT as revoked inside the in-memory store."""

        updated_token = replace(
            self.api_tokens[token_id],
            revoked_at=revoked_at,
            updated_at=revoked_at,
        )
        self.api_tokens[token_id] = updated_token
        return updated_token

    def commit(self) -> None:
        """Record successful transaction boundaries for assertions."""

        self.commit_calls += 1

    def rollback(self) -> None:
        """Match the production repository interface for service error handling."""

        return None
