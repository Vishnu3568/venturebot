"""Repository for ExperimentLearning persistence and queries (Step 13).

Strictly append-only: provides creation and retrieval only.
No update or delete operations exist.
"""

from __future__ import annotations

import json
from datetime import timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentLearningORM
from venturebot.learning.models import ExperimentLearning


class LearningRepository:
    """Repository managing ExperimentLearning records in SQLite."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def create(self, learning: ExperimentLearning) -> ExperimentLearning:
        """Persist a new ExperimentLearning record."""
        orm = ExperimentLearningORM(
            id=learning.id,
            experiment_id=learning.experiment_id,
            opportunity_id=learning.opportunity_id,
            decision_id=learning.decision_id,
            summary=learning.summary,
            what_worked=json.dumps(learning.what_worked),
            what_failed=json.dumps(learning.what_failed),
            key_learnings=json.dumps(learning.key_learnings),
            future_hypotheses=json.dumps(learning.future_hypotheses),
            recorded_at=learning.recorded_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    def get(self, learning_id: UUID) -> ExperimentLearning | None:
        """Retrieve an ExperimentLearning record by ID."""
        orm = self.session.get(ExperimentLearningORM, learning_id)
        return self._to_pydantic(orm) if orm is not None else None

    def list_for_experiment(self, experiment_id: UUID) -> list[ExperimentLearning]:
        """List all learning records for an experiment in reverse chronological order."""
        stmt = (
            select(ExperimentLearningORM)
            .where(ExperimentLearningORM.experiment_id == experiment_id)
            .order_by(
                ExperimentLearningORM.recorded_at.desc(),
                ExperimentLearningORM.id.asc(),
            )
        )
        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def list_for_opportunity(self, opportunity_id: UUID) -> list[ExperimentLearning]:
        """List all learning records for an opportunity in reverse chronological order."""
        stmt = (
            select(ExperimentLearningORM)
            .where(ExperimentLearningORM.opportunity_id == opportunity_id)
            .order_by(
                ExperimentLearningORM.recorded_at.desc(),
                ExperimentLearningORM.id.asc(),
            )
        )
        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def get_latest_for_experiment(self, experiment_id: UUID) -> ExperimentLearning | None:
        """Retrieve the most recently recorded learning record for an experiment."""
        stmt = (
            select(ExperimentLearningORM)
            .where(ExperimentLearningORM.experiment_id == experiment_id)
            .order_by(
                ExperimentLearningORM.recorded_at.desc(),
                ExperimentLearningORM.id.desc(),
            )
            .limit(1)
        )
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    @staticmethod
    def _to_pydantic(orm: ExperimentLearningORM) -> ExperimentLearning:
        """Map ExperimentLearningORM to canonical Pydantic contract."""
        recorded_at = orm.recorded_at
        if recorded_at is not None and recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)

        return ExperimentLearning(
            id=orm.id,
            experiment_id=orm.experiment_id,
            opportunity_id=orm.opportunity_id,
            decision_id=orm.decision_id,
            summary=orm.summary,
            what_worked=json.loads(orm.what_worked) if orm.what_worked else [],
            what_failed=json.loads(orm.what_failed) if orm.what_failed else [],
            key_learnings=json.loads(orm.key_learnings) if orm.key_learnings else [],
            future_hypotheses=json.loads(orm.future_hypotheses) if orm.future_hypotheses else [],
            recorded_at=recorded_at,
        )
