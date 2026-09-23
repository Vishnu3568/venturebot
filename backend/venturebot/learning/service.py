"""Experiment Learning Service for VentureBot (Step 13).

Governs recording and querying explicit, human/authorized experiment learning records.
Strictly preserves caller-supplied learnings without automatic learning generation,
decision mutation, or financial ledger side-effects.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.database.models import DecisionORM, ExperimentORM, OpportunityORM
from venturebot.database.repositories.learning import LearningRepository
from venturebot.learning.models import ExperimentLearning


class ExperimentLearningService:
    """Service governing structured retrospective experiment learning recording and queries."""

    @classmethod
    def record_learning(
        cls,
        session: Session,
        learning: ExperimentLearning,
        auto_commit: bool = True,
    ) -> ExperimentLearning:
        """Record an explicit learning record for an Experiment.

        Enforces:
        - Non-empty summary and at least one non-empty key learning.
        - Referential integrity: experiment and opportunity must exist.
        - Opportunity ID must match the experiment's associated opportunity ID.
        - Optional decision_id must reference an existing Decision if provided.
        - If decision_id is provided and decision has an experiment_id, it must match.
        - Preserves explicit lists (what_worked, what_failed, key_learnings, future_hypotheses).
        - Strictly read-only to financial and decision state:
          0 capital transactions, 0 decisions created, 0 status mutations.
        - Zero automated learning generation (no inference from metrics or LLMs).
        """
        # 1. Referential integrity: Experiment
        exp_orm = session.get(ExperimentORM, learning.experiment_id)
        if exp_orm is None:
            raise ValueError(f"Experiment '{learning.experiment_id}' does not exist.")

        # 2. Referential integrity: Opportunity
        opp_orm = session.get(OpportunityORM, learning.opportunity_id)
        if opp_orm is None:
            raise ValueError(f"Opportunity '{learning.opportunity_id}' does not exist.")

        if learning.opportunity_id != exp_orm.opportunity_id:
            raise ValueError(
                f"Learning opportunity_id '{learning.opportunity_id}' does not match "
                f"experiment's opportunity_id '{exp_orm.opportunity_id}'."
            )

        # 3. Referential integrity: Optional Decision
        if learning.decision_id is not None:
            dec_orm = session.get(DecisionORM, learning.decision_id)
            if dec_orm is None:
                raise ValueError(f"Decision '{learning.decision_id}' does not exist.")
            if dec_orm.experiment_id is not None and dec_orm.experiment_id != learning.experiment_id:
                raise ValueError(
                    f"Decision '{learning.decision_id}' is associated with experiment "
                    f"'{dec_orm.experiment_id}', not '{learning.experiment_id}'."
                )

        # 4. Clean and validate contract fields
        clean_summary = learning.summary.strip() if learning.summary else ""
        if not clean_summary:
            raise ValueError("Learning summary must not be empty.")

        cleaned_key_learnings = [
            item.strip() for item in learning.key_learnings if item and item.strip()
        ]
        if not cleaned_key_learnings:
            raise ValueError("At least one non-empty key learning is required.")

        cleaned_what_worked = [
            item.strip() for item in learning.what_worked if item and item.strip()
        ]
        cleaned_what_failed = [
            item.strip() for item in learning.what_failed if item and item.strip()
        ]
        cleaned_future_hypotheses = [
            item.strip() for item in learning.future_hypotheses if item and item.strip()
        ]

        validated_learning = ExperimentLearning(
            id=learning.id,
            experiment_id=learning.experiment_id,
            opportunity_id=learning.opportunity_id,
            decision_id=learning.decision_id,
            summary=clean_summary,
            what_worked=cleaned_what_worked,
            what_failed=cleaned_what_failed,
            key_learnings=cleaned_key_learnings,
            future_hypotheses=cleaned_future_hypotheses,
            recorded_at=learning.recorded_at,
        )

        repo = LearningRepository(session, auto_commit=auto_commit)
        return repo.create(validated_learning)

    @classmethod
    def get_learning(
        cls,
        session: Session,
        learning_id: UUID,
    ) -> ExperimentLearning | None:
        """Retrieve a specific learning record by ID."""
        repo = LearningRepository(session, auto_commit=False)
        return repo.get(learning_id)

    @classmethod
    def list_learnings_for_experiment(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> list[ExperimentLearning]:
        """List all learning records for an experiment in reverse chronological order."""
        repo = LearningRepository(session, auto_commit=False)
        return repo.list_for_experiment(experiment_id)

    @classmethod
    def list_learnings_for_opportunity(
        cls,
        session: Session,
        opportunity_id: UUID,
    ) -> list[ExperimentLearning]:
        """List all learning records for an opportunity in reverse chronological order."""
        repo = LearningRepository(session, auto_commit=False)
        return repo.list_for_opportunity(opportunity_id)

    @classmethod
    def get_latest_learning_for_experiment(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> ExperimentLearning | None:
        """Retrieve the most recently recorded learning record for an experiment."""
        repo = LearningRepository(session, auto_commit=False)
        return repo.get_latest_for_experiment(experiment_id)
