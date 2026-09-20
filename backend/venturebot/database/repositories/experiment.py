"""Repository for Experiment persistence, queries, and financial source-of-truth synchronization."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from venturebot.database.models import CapitalTransactionORM, ExperimentORM
from venturebot.models.capital import TransactionType
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod


class ExperimentRepository:
    """Repository managing Experiment persistence in the SQLite database.
    
    Preserves the CapitalTransaction ledger as the authoritative source of truth
    for actual experiment spending.
    """

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def get_actual_spend_from_ledger(self, experiment_id: UUID) -> Decimal:
        """Calculate total actual spend for an experiment directly from the capital ledger.
        
        The financial ledger is the single source of truth for all money disbursed.
        """
        stmt = select(func.coalesce(func.sum(CapitalTransactionORM.amount), Decimal("0.00"))).where(
            CapitalTransactionORM.experiment_id == experiment_id,
            CapitalTransactionORM.transaction_type == TransactionType.EXPERIMENT_SPEND.value,
        )
        result = self.session.scalar(stmt)
        if result is None:
            return Decimal("0.00")
        return result if isinstance(result, Decimal) else Decimal(str(result))

    def create(self, experiment: Experiment) -> Experiment:
        """Persist a new Experiment."""
        orm = ExperimentORM(
            id=experiment.id,
            opportunity_id=experiment.opportunity_id,
            hypothesis=experiment.hypothesis,
            objective=experiment.objective,
            channel=experiment.channel.value,
            monetization_method=experiment.monetization_method.value,
            allocated_budget=experiment.allocated_budget,
            max_allowed_spend=experiment.max_allowed_spend,
            actual_spend=experiment.actual_spend,
            success_criteria=experiment.success_criteria,
            failure_criteria=experiment.failure_criteria,
            planned_start=experiment.planned_start,
            planned_end=experiment.planned_end,
            actual_start=experiment.actual_start,
            actual_end=experiment.actual_end,
            status=experiment.status.value,
            created_at=experiment.created_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm, actual_spend=experiment.actual_spend)

    def get(self, experiment_id: UUID, sync_spend_from_ledger: bool = True) -> Experiment | None:
        """Retrieve an Experiment by unique ID with optional ledger spend hydration."""
        orm = self.session.get(ExperimentORM, experiment_id)
        if orm is None:
            return None

        actual_spend = None
        if sync_spend_from_ledger:
            actual_spend = self.get_actual_spend_from_ledger(experiment_id)

        return self._to_pydantic(orm, actual_spend=actual_spend)

    def list(
        self,
        opportunity_id: UUID | None = None,
        status: ExperimentStatus | str | None = None,
        sync_spend_from_ledger: bool = True,
    ) -> list[Experiment]:
        """List experiments with optional filtering by opportunity_id and status."""
        stmt = select(ExperimentORM).order_by(
            ExperimentORM.created_at.desc(),
            ExperimentORM.id.asc(),
        )
        if opportunity_id is not None:
            stmt = stmt.where(ExperimentORM.opportunity_id == opportunity_id)
        if status is not None:
            status_val = status.value if isinstance(status, ExperimentStatus) else str(status)
            stmt = stmt.where(ExperimentORM.status == status_val)

        records = self.session.scalars(stmt).all()
        results = []
        for r in records:
            actual_spend = self.get_actual_spend_from_ledger(r.id) if sync_spend_from_ledger else None
            results.append(self._to_pydantic(r, actual_spend=actual_spend))
        return results

    def update_status(self, experiment_id: UUID, status: ExperimentStatus | str) -> Experiment | None:
        """Update the status of an existing Experiment."""
        orm = self.session.get(ExperimentORM, experiment_id)
        if orm is None:
            return None

        status_val = status.value if isinstance(status, ExperimentStatus) else str(status)
        orm.status = status_val
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        actual_spend = self.get_actual_spend_from_ledger(experiment_id)
        return self._to_pydantic(orm, actual_spend=actual_spend)

    @staticmethod
    def _to_pydantic(orm: ExperimentORM, actual_spend: Decimal | None = None) -> Experiment:
        """Map ExperimentORM to canonical Pydantic model."""
        created_at = orm.created_at
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        planned_start = orm.planned_start
        if planned_start is not None and planned_start.tzinfo is None:
            planned_start = planned_start.replace(tzinfo=timezone.utc)

        planned_end = orm.planned_end
        if planned_end is not None and planned_end.tzinfo is None:
            planned_end = planned_end.replace(tzinfo=timezone.utc)

        actual_start = orm.actual_start
        if actual_start is not None and actual_start.tzinfo is None:
            actual_start = actual_start.replace(tzinfo=timezone.utc)

        actual_end = orm.actual_end
        if actual_end is not None and actual_end.tzinfo is None:
            actual_end = actual_end.replace(tzinfo=timezone.utc)

        spend = (
            actual_spend
            if actual_spend is not None
            else (
                orm.actual_spend
                if isinstance(orm.actual_spend, Decimal)
                else Decimal(str(orm.actual_spend or 0))
            )
        )

        return Experiment(
            id=orm.id,
            opportunity_id=orm.opportunity_id,
            hypothesis=orm.hypothesis,
            objective=orm.objective,
            channel=Channel(orm.channel),
            monetization_method=MonetizationMethod(orm.monetization_method),
            allocated_budget=(
                orm.allocated_budget
                if isinstance(orm.allocated_budget, Decimal)
                else Decimal(str(orm.allocated_budget))
            ),
            max_allowed_spend=(
                orm.max_allowed_spend
                if isinstance(orm.max_allowed_spend, Decimal)
                else Decimal(str(orm.max_allowed_spend))
            ),
            actual_spend=spend,
            success_criteria=orm.success_criteria,
            failure_criteria=orm.failure_criteria,
            planned_start=planned_start,
            planned_end=planned_end,
            actual_start=actual_start,
            actual_end=actual_end,
            status=ExperimentStatus(orm.status),
            created_at=created_at,
        )
