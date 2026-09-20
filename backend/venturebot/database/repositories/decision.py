"""Repository for Decision persistence and queries."""

from __future__ import annotations

from datetime import timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import DecisionORM
from venturebot.models.decision import Decision, DecisionOutcome


class DecisionRepository:
    """Repository managing Decision records in the SQLite database."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def create(self, decision: Decision) -> Decision:
        """Persist a new Decision record."""
        orm = DecisionORM(
            id=decision.id,
            opportunity_id=decision.opportunity_id,
            experiment_id=decision.experiment_id,
            outcome=decision.outcome.value,
            reason=decision.reason,
            evidence_summary=decision.evidence_summary,
            confidence=decision.confidence,
            decided_at=decision.decided_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    def get(self, decision_id: UUID) -> Decision | None:
        """Retrieve a Decision by unique ID."""
        orm = self.session.get(DecisionORM, decision_id)
        return self._to_pydantic(orm) if orm is not None else None

    def list(
        self,
        opportunity_id: UUID | None = None,
        experiment_id: UUID | None = None,
        outcome: DecisionOutcome | str | None = None,
    ) -> list[Decision]:
        """List decisions with optional filtering by opportunity, experiment, or outcome."""
        stmt = select(DecisionORM).order_by(
            DecisionORM.decided_at.desc(),
            DecisionORM.id.asc(),
        )
        if opportunity_id is not None:
            stmt = stmt.where(DecisionORM.opportunity_id == opportunity_id)
        if experiment_id is not None:
            stmt = stmt.where(DecisionORM.experiment_id == experiment_id)
        if outcome is not None:
            outcome_val = outcome.value if isinstance(outcome, DecisionOutcome) else str(outcome)
            stmt = stmt.where(DecisionORM.outcome == outcome_val)

        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def get_latest_for_experiment(self, experiment_id: UUID) -> Decision | None:
        """Retrieve the most recently recorded decision for an experiment."""
        stmt = (
            select(DecisionORM)
            .where(DecisionORM.experiment_id == experiment_id)
            .order_by(DecisionORM.decided_at.desc(), DecisionORM.id.desc())
            .limit(1)
        )
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    def get_latest_for_opportunity(self, opportunity_id: UUID) -> Decision | None:
        """Retrieve the most recently recorded decision for an opportunity."""
        stmt = (
            select(DecisionORM)
            .where(DecisionORM.opportunity_id == opportunity_id)
            .order_by(DecisionORM.decided_at.desc(), DecisionORM.id.desc())
            .limit(1)
        )
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    @staticmethod
    def _to_pydantic(orm: DecisionORM) -> Decision:
        """Map DecisionORM to canonical Pydantic model."""
        decided_at = orm.decided_at
        if decided_at is not None and decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=timezone.utc)

        return Decision(
            id=orm.id,
            opportunity_id=orm.opportunity_id,
            experiment_id=orm.experiment_id,
            outcome=DecisionOutcome(orm.outcome),
            reason=orm.reason,
            evidence_summary=orm.evidence_summary or "",
            confidence=orm.confidence,
            decided_at=decided_at,
        )
