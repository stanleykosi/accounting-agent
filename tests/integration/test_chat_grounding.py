"""
Purpose: Integration tests for the chat grounding service that resolves
entity/close-run/period context for chat threads.
Scope: Grounding context resolution, access verification, context serialization,
and canonical error handling for missing or inaccessible resources.
Dependencies: pytest, chat grounding service, entity and close-run repositories,
and canonical enum definitions.

Test matrix:
1. Grounding resolves valid entity context without close run.
2. Grounding resolves valid entity + close run context with period label.
3. Grounding raises access error when entity is inaccessible.
4. Grounding raises not-found error when close run is inaccessible.
5. Context payload serialization/deserialization is round-trip safe.
6. Period label formatting handles single-month and multi-month ranges.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from services.auth.service import serialize_uuid
from services.chat.grounding import (
    ChatGroundingError,
    ChatGroundingErrorCode,
    ChatGroundingService,
    GroundingContextRecord,
)
from services.common.enums import AutonomyMode
from services.contracts.chat_models import GroundingContext
from services.db.models.close_run import CloseRunStatus
from services.db.models.entity import EntityStatus
from services.db.repositories.close_run_repo import (
    CloseRunAccessRecord,
    CloseRunEntityRecord,
    CloseRunRecord,
)
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityMembershipRecord,
    EntityRecord,
    EntityUserRecord,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_user() -> EntityUserRecord:
    """Provide a canonical test user record."""
    return EntityUserRecord(
        id=uuid4(),
        email="test@example.com",
        full_name="Test User",
    )


@pytest.fixture()
def sample_membership(sample_user: EntityUserRecord) -> EntityMembershipRecord:
    """Provide a canonical test membership record."""
    return EntityMembershipRecord(
        id=uuid4(),
        entity_id=uuid4(),
        user_id=sample_user.id,
        role="owner",
        is_default_actor=True,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        user=sample_user,
    )


@pytest.fixture()
def sample_entity(sample_membership: EntityMembershipRecord) -> EntityRecord:
    """Provide a canonical test entity record with NGN default."""
    return EntityRecord(
        id=uuid4(),
        name="Test Entity",
        legal_name="Test Entity Limited",
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        default_confidence_thresholds={"extraction": 0.85},
        status=EntityStatus.ACTIVE,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )


@pytest.fixture()
def sample_entity_access(
    sample_entity: EntityRecord,
    sample_membership: EntityMembershipRecord,
) -> EntityAccessRecord:
    """Provide a canonical test entity access record."""
    return EntityAccessRecord(
        entity=sample_entity,
        membership=sample_membership,
    )


@pytest.fixture()
def sample_close_run_entity() -> CloseRunEntityRecord:
    """Provide a canonical test close-run entity record."""
    return CloseRunEntityRecord(
        id=uuid4(),
        name="Test Entity",
        base_currency="NGN",
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        status=EntityStatus.ACTIVE,
    )


@pytest.fixture()
def sample_close_run(sample_close_run_entity: CloseRunEntityRecord) -> CloseRunRecord:
    """Provide a canonical test close run record."""
    return CloseRunRecord(
        id=uuid4(),
        entity_id=sample_close_run_entity.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        status=CloseRunStatus.DRAFT,
        reporting_currency="NGN",
        current_version_no=1,
        opened_by_user_id=uuid4(),
        approved_by_user_id=None,
        approved_at=None,
        archived_at=None,
        reopened_from_close_run_id=None,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )


@pytest.fixture()
def sample_close_run_access(
    sample_close_run: CloseRunRecord,
    sample_close_run_entity: CloseRunEntityRecord,
) -> CloseRunAccessRecord:
    """Provide a canonical test close run access record."""
    return CloseRunAccessRecord(
        close_run=sample_close_run,
        entity=sample_close_run_entity,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_grounding_resolves_entity_without_close_run(
    sample_entity_access: EntityAccessRecord,
    sample_user: EntityUserRecord,
) -> None:
    """Grounding context resolves valid entity context without close run."""
    entity_repo = MagicMock()
    entity_repo.get_entity_for_user.return_value = sample_entity_access

    close_run_repo = MagicMock()

    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    result = service.resolve_context(
        entity_id=sample_entity_access.entity.id,
        close_run_id=None,
        user_id=sample_user.id,
    )

    assert isinstance(result, GroundingContextRecord)
    assert result.entity.id == sample_entity_access.entity.id
    assert result.close_run is None
    assert result.context.entity_id == serialize_uuid(sample_entity_access.entity.id)
    assert result.context.entity_name == sample_entity_access.entity.name
    assert result.context.close_run_id is None
    assert result.context.period_label is None
    assert result.context.autonomy_mode == AutonomyMode.HUMAN_REVIEW.value
    assert result.context.base_currency == "NGN"


def test_grounding_resolves_entity_and_close_run(
    sample_entity_access: EntityAccessRecord,
    sample_close_run_access: CloseRunAccessRecord,
    sample_user: EntityUserRecord,
) -> None:
    """Grounding context resolves valid entity + close run with period label."""
    entity_repo = MagicMock()
    entity_repo.get_entity_for_user.return_value = sample_entity_access

    close_run_repo = MagicMock()
    close_run_repo.get_close_run_for_user.return_value = sample_close_run_access

    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    result = service.resolve_context(
        entity_id=sample_entity_access.entity.id,
        close_run_id=sample_close_run_access.close_run.id,
        user_id=sample_user.id,
    )

    assert isinstance(result, GroundingContextRecord)
    assert result.close_run is not None
    assert result.close_run.id == sample_close_run_access.close_run.id
    assert result.context.close_run_id == serialize_uuid(sample_close_run_access.close_run.id)
    assert result.context.period_label == "Jan 2025"


def test_grounding_raises_on_inaccessible_entity(
    sample_user: EntityUserRecord,
) -> None:
    """Grounding raises canonical access error when entity is inaccessible."""
    entity_repo = MagicMock()
    entity_repo.get_entity_for_user.return_value = None

    close_run_repo = MagicMock()

    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    with pytest.raises(ChatGroundingError) as exc_info:
        service.resolve_context(
            entity_id=uuid4(),
            close_run_id=None,
            user_id=sample_user.id,
        )

    error = exc_info.value
    assert error.status_code == 404
    assert error.code == ChatGroundingErrorCode.ACCESS_DENIED


def test_grounding_raises_on_inaccessible_close_run(
    sample_entity_access: EntityAccessRecord,
    sample_user: EntityUserRecord,
) -> None:
    """Grounding raises canonical not-found error when close run is inaccessible."""
    entity_repo = MagicMock()
    entity_repo.get_entity_for_user.return_value = sample_entity_access

    close_run_repo = MagicMock()
    close_run_repo.get_close_run_for_user.return_value = None

    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    with pytest.raises(ChatGroundingError) as exc_info:
        service.resolve_context(
            entity_id=sample_entity_access.entity.id,
            close_run_id=uuid4(),
            user_id=sample_user.id,
        )

    error = exc_info.value
    assert error.status_code == 404
    assert error.code == ChatGroundingErrorCode.CLOSE_RUN_NOT_FOUND


def test_context_payload_round_trip() -> None:
    """Context payload serialization/deserialization is round-trip safe."""
    entity_repo = MagicMock()
    close_run_repo = MagicMock()

    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    original = GroundingContext(
        entity_id=str(uuid4()),
        entity_name="Test Entity",
        close_run_id=str(uuid4()),
        period_label="Jan 2025",
        autonomy_mode="human_review",
        base_currency="NGN",
    )

    payload = service.build_context_payload(context=original)
    restored = service.parse_context_payload(payload=payload)

    assert restored == original


def test_period_label_single_month() -> None:
    """Period label is a single month when start and end are in the same month."""
    entity_repo = MagicMock()
    close_run_repo = MagicMock()
    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    entity = EntityRecord(
        id=uuid4(),
        name="Test",
        legal_name=None,
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        default_confidence_thresholds={},
        status=EntityStatus.ACTIVE,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )
    close_run = CloseRunRecord(
        id=uuid4(),
        entity_id=entity.id,
        period_start=date(2025, 3, 1),
        period_end=date(2025, 3, 31),
        status=CloseRunStatus.DRAFT,
        reporting_currency="NGN",
        current_version_no=1,
        opened_by_user_id=uuid4(),
        approved_by_user_id=None,
        approved_at=None,
        archived_at=None,
        reopened_from_close_run_id=None,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )

    context = service._build_grounding_context(entity=entity, close_run=close_run)
    assert context.period_label == "Mar 2025"


def test_period_label_multi_month_range() -> None:
    """Period label shows a range when start and end span multiple months."""
    entity_repo = MagicMock()
    close_run_repo = MagicMock()
    service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )

    entity = EntityRecord(
        id=uuid4(),
        name="Test",
        legal_name=None,
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        default_confidence_thresholds={},
        status=EntityStatus.ACTIVE,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )
    close_run = CloseRunRecord(
        id=uuid4(),
        entity_id=entity.id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        status=CloseRunStatus.DRAFT,
        reporting_currency="NGN",
        current_version_no=1,
        opened_by_user_id=uuid4(),
        approved_by_user_id=None,
        approved_at=None,
        archived_at=None,
        reopened_from_close_run_id=None,
        created_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
        updated_at=__import__("datetime", fromlist=["datetime"]).datetime.now(
            tz=__import__("datetime", fromlist=["timezone"]).timezone.utc
        ),
    )

    context = service._build_grounding_context(entity=entity, close_run=close_run)
    assert context.period_label == "Jan 2025 - Mar 2025"
