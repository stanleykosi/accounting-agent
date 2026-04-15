"""
Purpose: Persist and query recommendations, journal entries, and journal lines.
Scope: Recommendation status updates, journal CRUD, journal line persistence,
sequence number generation, and close-run/entity scoped queries.
Dependencies: SQLAlchemy ORM sessions, recommendation and journal persistence models,
canonical enums, and UUID serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from services.common.enums import ReviewStatus
from services.db.models.exports import Artifact
from services.db.models.journals import JournalEntry, JournalLine, JournalPosting
from services.db.models.recommendations import Recommendation
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    """Describe one recommendation row as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    document_id: UUID | None
    recommendation_type: str
    status: str
    payload: dict[str, Any]
    confidence: float
    reasoning_summary: str
    evidence_links: list[dict[str, Any]]
    prompt_version: str
    rule_version: str
    schema_version: str
    created_by_system: bool
    autonomy_mode: str | None
    superseded_by_id: UUID | None
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class JournalEntryRecord:
    """Describe one journal entry row as an immutable service-layer record."""

    id: UUID
    entity_id: UUID
    close_run_id: UUID
    recommendation_id: UUID | None
    journal_number: str
    posting_date: date
    status: str
    description: str
    total_debits: float
    total_credits: float
    line_count: int
    source_surface: str
    autonomy_mode: str | None
    reasoning_summary: str | None
    metadata_payload: dict[str, Any]
    approved_by_user_id: UUID | None
    applied_by_user_id: UUID | None
    superseded_by_id: UUID | None
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class JournalLineRecord:
    """Describe one journal line row as an immutable service-layer record."""

    id: UUID
    journal_entry_id: UUID
    line_no: int
    account_code: str
    line_type: str
    amount: float
    description: str | None
    dimensions: dict[str, Any]
    reference: str | None
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class JournalPostingRecord:
    """Describe one posted journal outcome and any linked artifact reference."""

    id: UUID
    journal_entry_id: UUID
    entity_id: UUID
    close_run_id: UUID
    version_no: int
    posting_target: str
    provider: str | None
    status: str
    artifact_id: UUID | None
    artifact_type: str | None
    artifact_filename: str | None
    artifact_storage_key: str | None
    note: str | None
    posting_metadata: dict[str, Any]
    posted_by_user_id: UUID | None
    posted_at: Any
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class JournalWithLinesResult:
    """Describe a journal entry with its attached lines for API response assembly."""

    entry: JournalEntryRecord
    lines: tuple[JournalLineRecord, ...]
    postings: tuple[JournalPostingRecord, ...] = ()


class RecommendationJournalRepository:
    """Execute canonical recommendation and journal persistence in one DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the SQLAlchemy session used by recommendation and journal workflows."""
        self._db_session = db_session

    # ------------------------------------------------------------------
    # Recommendation queries and mutations
    # ------------------------------------------------------------------

    def get_recommendation(
        self,
        *,
        recommendation_id: UUID,
    ) -> RecommendationRecord | None:
        """Return a recommendation by ID or None."""
        statement = select(Recommendation).where(
            Recommendation.id == recommendation_id,
        )
        rec = self._db_session.execute(statement).scalar_one_or_none()
        return _map_recommendation(rec) if rec is not None else None

    def list_recommendations_for_close_run(
        self,
        *,
        close_run_id: UUID,
        status: ReviewStatus | None = None,
    ) -> tuple[RecommendationRecord, ...]:
        """Return recommendations for a close run, optionally filtered by status."""
        statement = select(Recommendation).where(
            Recommendation.close_run_id == close_run_id,
        )
        if status is not None:
            statement = statement.where(Recommendation.status == status.value)
        statement = statement.order_by(desc(Recommendation.created_at))
        return tuple(
            _map_recommendation(rec) for rec in self._db_session.scalars(statement)
        )

    def update_recommendation_status(
        self,
        *,
        recommendation_id: UUID,
        status: str,
        superseded_by_id: UUID | None = None,
    ) -> RecommendationRecord:
        """Update a recommendation's review status."""
        rec = self._load_recommendation(recommendation_id)
        rec.status = status
        if superseded_by_id is not None:
            rec.superseded_by_id = superseded_by_id
        self._db_session.flush()
        return _map_recommendation(rec)

    # ------------------------------------------------------------------
    # Journal entry CRUD
    # ------------------------------------------------------------------

    def create_journal_entry(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        recommendation_id: UUID | None,
        journal_number: str,
        posting_date: date,
        status: str,
        description: str,
        total_debits: float,
        total_credits: float,
        line_count: int,
        source_surface: str,
        autonomy_mode: str | None,
        reasoning_summary: str | None,
        metadata_payload: dict[str, Any],
    ) -> JournalEntry:
        """Persist a journal entry header and return its ORM instance."""
        entry = JournalEntry(
            entity_id=entity_id,
            close_run_id=close_run_id,
            recommendation_id=recommendation_id,
            journal_number=journal_number,
            posting_date=posting_date,
            status=status,
            description=description,
            total_debits=total_debits,
            total_credits=total_credits,
            line_count=line_count,
            source_surface=source_surface,
            autonomy_mode=autonomy_mode,
            reasoning_summary=reasoning_summary,
            metadata_payload=metadata_payload,
        )
        self._db_session.add(entry)
        self._db_session.flush()
        return entry

    def get_journal_entry(
        self,
        *,
        journal_id: UUID,
    ) -> JournalWithLinesResult | None:
        """Return a journal entry by ID with its lines, or None."""
        entry = self._db_session.execute(
            select(JournalEntry).where(JournalEntry.id == journal_id),
        ).scalar_one_or_none()
        if entry is None:
            return None

        lines = self._db_session.scalars(
            select(JournalLine)
            .where(JournalLine.journal_entry_id == journal_id)
            .order_by(JournalLine.line_no),
        ).all()
        postings = self.list_postings_for_journal(journal_entry_id=journal_id)

        return JournalWithLinesResult(
            entry=_map_journal_entry(entry),
            lines=tuple(_map_journal_line(line) for line in lines),
            postings=postings,
        )

    def list_journals_for_close_run(
        self,
        *,
        close_run_id: UUID,
        status: ReviewStatus | None = None,
    ) -> tuple[JournalEntryRecord, ...]:
        """Return journal entries for a close run, optionally filtered by status."""
        statement = select(JournalEntry).where(
            JournalEntry.close_run_id == close_run_id,
        )
        if status is not None:
            statement = statement.where(JournalEntry.status == status.value)
        statement = statement.order_by(desc(JournalEntry.created_at))
        return tuple(
            _map_journal_entry(entry) for entry in self._db_session.scalars(statement)
        )

    def list_journals_for_recommendation(
        self,
        *,
        recommendation_id: UUID,
    ) -> tuple[JournalEntryRecord, ...]:
        """Return journal entries generated from a specific recommendation."""
        statement = (
            select(JournalEntry)
            .where(JournalEntry.recommendation_id == recommendation_id)
            .order_by(desc(JournalEntry.created_at))
        )
        return tuple(
            _map_journal_entry(entry) for entry in self._db_session.scalars(statement)
        )

    def update_journal_status(
        self,
        *,
        journal_id: UUID,
        status: str,
        approved_by_user_id: UUID | None = None,
        applied_by_user_id: UUID | None = None,
        superseded_by_id: UUID | None = None,
    ) -> JournalEntryRecord:
        """Update a journal entry's review status."""
        entry = self._load_journal_entry(journal_id)
        entry.status = status
        if approved_by_user_id is not None:
            entry.approved_by_user_id = approved_by_user_id
        if applied_by_user_id is not None:
            entry.applied_by_user_id = applied_by_user_id
        if superseded_by_id is not None:
            entry.superseded_by_id = superseded_by_id
        self._db_session.flush()
        return _map_journal_entry(entry)

    def create_journal_lines(
        self,
        *,
        journal_entry_id: UUID,
        lines: list[dict[str, Any]],
    ) -> int:
        """Persist journal line items and return the count created."""
        rows = [
            JournalLine(
                journal_entry_id=journal_entry_id,
                line_no=line["line_no"],
                account_code=line["account_code"],
                line_type=line["line_type"],
                amount=line["amount"],
                description=line.get("description"),
                dimensions=line.get("dimensions", {}),
                reference=line.get("reference"),
            )
            for line in lines
        ]
        self._db_session.add_all(rows)
        self._db_session.flush()
        return len(rows)

    def get_journal_lines(
        self,
        *,
        journal_entry_id: UUID,
    ) -> tuple[JournalLineRecord, ...]:
        """Return all lines for a journal entry in line_no order."""
        statement = (
            select(JournalLine)
            .where(JournalLine.journal_entry_id == journal_entry_id)
            .order_by(JournalLine.line_no)
        )
        return tuple(
            _map_journal_line(line) for line in self._db_session.scalars(statement)
        )

    def create_journal_posting(
        self,
        *,
        journal_entry_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        version_no: int,
        posting_target: str,
        provider: str | None,
        status: str,
        artifact_id: UUID | None,
        artifact_type: str | None,
        note: str | None,
        posting_metadata: dict[str, Any],
        posted_by_user_id: UUID | None,
        posted_at: Any,
    ) -> JournalPostingRecord:
        """Persist the canonical posting record for one journal entry."""

        posting = JournalPosting(
            journal_entry_id=journal_entry_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            version_no=version_no,
            posting_target=posting_target,
            provider=provider,
            status=status,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            note=note,
            posting_metadata=posting_metadata,
            posted_by_user_id=posted_by_user_id,
            posted_at=posted_at,
        )
        self._db_session.add(posting)
        self._db_session.flush()
        artifact = self._db_session.get(Artifact, artifact_id) if artifact_id is not None else None
        return _map_journal_posting(posting, artifact=artifact)

    def list_postings_for_journal(
        self,
        *,
        journal_entry_id: UUID,
    ) -> tuple[JournalPostingRecord, ...]:
        """Return posting records for one journal entry in newest-first order."""

        rows = self._db_session.execute(
            select(JournalPosting, Artifact)
            .outerjoin(Artifact, Artifact.id == JournalPosting.artifact_id)
            .where(JournalPosting.journal_entry_id == journal_entry_id)
            .order_by(desc(JournalPosting.posted_at), desc(JournalPosting.created_at))
        ).all()
        return tuple(_map_journal_posting(posting, artifact=artifact) for posting, artifact in rows)

    def list_postings_for_journal_ids(
        self,
        *,
        journal_entry_ids: tuple[UUID, ...],
    ) -> dict[UUID, tuple[JournalPostingRecord, ...]]:
        """Return posting records grouped by journal entry for a batch of journals."""

        if not journal_entry_ids:
            return {}

        grouped: dict[UUID, list[JournalPostingRecord]] = {}
        rows = self._db_session.execute(
            select(JournalPosting, Artifact)
            .outerjoin(Artifact, Artifact.id == JournalPosting.artifact_id)
            .where(JournalPosting.journal_entry_id.in_(journal_entry_ids))
            .order_by(desc(JournalPosting.posted_at), desc(JournalPosting.created_at))
        ).all()
        for posting, artifact in rows:
            grouped.setdefault(posting.journal_entry_id, []).append(
                _map_journal_posting(posting, artifact=artifact)
            )
        return {journal_id: tuple(records) for journal_id, records in grouped.items()}

    # ------------------------------------------------------------------
    # Sequence number generation
    # ------------------------------------------------------------------

    def get_next_journal_sequence_no(
        self,
        *,
        entity_id: UUID,
        posting_date: date,
    ) -> int:
        """Return the next journal sequence number for an entity in a given year."""
        year_start = date(posting_date.year, 1, 1)
        year_end = date(posting_date.year, 12, 31)
        statement = (
            select(func.max(JournalEntry.journal_number))
            .where(
                JournalEntry.entity_id == entity_id,
                JournalEntry.posting_date >= year_start,
                JournalEntry.posting_date <= year_end,
            )
        )
        max_journal = self._db_session.execute(statement).scalar_one_or_none()
        if max_journal is None:
            return 1
        # Extract sequence from format JE-YYYY-NNNNN
        parts = str(max_journal).split("-")
        if len(parts) >= 3:
            try:
                return int(parts[-1]) + 1
            except ValueError:
                pass
        return 1

    # ------------------------------------------------------------------
    # Transaction management
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """Commit the current transaction after a successful mutation."""
        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current transaction after a failed mutation."""
        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the exception originated from a DB integrity failure."""
        return isinstance(error, IntegrityError)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_recommendation(self, recommendation_id: UUID) -> Recommendation:
        """Load a recommendation or fail fast."""
        rec = self._db_session.execute(
            select(Recommendation).where(Recommendation.id == recommendation_id),
        ).scalar_one_or_none()
        if rec is None:
            raise LookupError(f"Recommendation {recommendation_id} does not exist.")
        return rec

    def _load_journal_entry(self, journal_id: UUID) -> JournalEntry:
        """Load a journal entry or fail fast."""
        entry = self._db_session.execute(
            select(JournalEntry).where(JournalEntry.id == journal_id),
        ).scalar_one_or_none()
        if entry is None:
            raise LookupError(f"Journal entry {journal_id} does not exist.")
        return entry


def _map_recommendation(rec: Recommendation) -> RecommendationRecord:
    """Convert an ORM recommendation row into an immutable service record."""
    return RecommendationRecord(
        id=rec.id,
        close_run_id=rec.close_run_id,
        document_id=rec.document_id,
        recommendation_type=rec.recommendation_type,
        status=rec.status,
        payload=rec.payload,
        confidence=rec.confidence,
        reasoning_summary=rec.reasoning_summary,
        evidence_links=rec.evidence_links,
        prompt_version=rec.prompt_version,
        rule_version=rec.rule_version,
        schema_version=rec.schema_version,
        created_by_system=rec.created_by_system,
        autonomy_mode=rec.autonomy_mode,
        superseded_by_id=rec.superseded_by_id,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _map_journal_entry(entry: JournalEntry) -> JournalEntryRecord:
    """Convert an ORM journal entry row into an immutable service record."""
    return JournalEntryRecord(
        id=entry.id,
        entity_id=entry.entity_id,
        close_run_id=entry.close_run_id,
        recommendation_id=entry.recommendation_id,
        journal_number=entry.journal_number,
        posting_date=entry.posting_date,
        status=entry.status,
        description=entry.description,
        total_debits=entry.total_debits,
        total_credits=entry.total_credits,
        line_count=entry.line_count,
        source_surface=entry.source_surface,
        autonomy_mode=entry.autonomy_mode,
        reasoning_summary=entry.reasoning_summary,
        metadata_payload=entry.metadata_payload,
        approved_by_user_id=entry.approved_by_user_id,
        applied_by_user_id=entry.applied_by_user_id,
        superseded_by_id=entry.superseded_by_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _map_journal_line(line: JournalLine) -> JournalLineRecord:
    """Convert an ORM journal line row into an immutable service record."""
    return JournalLineRecord(
        id=line.id,
        journal_entry_id=line.journal_entry_id,
        line_no=line.line_no,
        account_code=line.account_code,
        line_type=line.line_type,
        amount=line.amount,
        description=line.description,
        dimensions=line.dimensions,
        reference=line.reference,
        created_at=line.created_at,
        updated_at=line.updated_at,
    )


def _map_journal_posting(
    posting: JournalPosting,
    *,
    artifact: Artifact | None,
) -> JournalPostingRecord:
    """Convert an ORM journal posting row into an immutable service record."""

    artifact_metadata = dict(artifact.artifact_metadata) if artifact is not None else {}
    return JournalPostingRecord(
        id=posting.id,
        journal_entry_id=posting.journal_entry_id,
        entity_id=posting.entity_id,
        close_run_id=posting.close_run_id,
        version_no=posting.version_no,
        posting_target=posting.posting_target,
        provider=posting.provider,
        status=posting.status,
        artifact_id=posting.artifact_id,
        artifact_type=posting.artifact_type,
        artifact_filename=(
            str(artifact_metadata.get("filename"))
            if artifact_metadata.get("filename") is not None
            else None
        ),
        artifact_storage_key=artifact.storage_key if artifact is not None else None,
        note=posting.note,
        posting_metadata=dict(posting.posting_metadata),
        posted_by_user_id=posting.posted_by_user_id,
        posted_at=posting.posted_at,
        created_at=posting.created_at,
        updated_at=posting.updated_at,
    )


__all__ = [
    "JournalEntryRecord",
    "JournalLineRecord",
    "JournalPostingRecord",
    "JournalWithLinesResult",
    "RecommendationJournalRepository",
    "RecommendationRecord",
]
