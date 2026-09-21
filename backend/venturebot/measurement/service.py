"""Experiment Measurement & Result Recording Foundation Service for VentureBot.

Governs recording and retrieving measured experiment outcomes and evidence
classifications (FACT, INFERENCE, HYPOTHESIS, PREDICTION).
Isolated completely from the financial ledger (zero capital mutations, zero spend side-effects).
Does NOT make automated decisions (no automatic SCALE, ITERATE, KILL, or HOLD).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import ExperimentStatus
from venturebot.models.metrics import ExperimentMetrics


class ExperimentMeasurementService:
    """Service governing experiment measurements and evidence recording."""

    @classmethod
    def record_measurement(
        cls,
        session: Session,
        metrics: ExperimentMetrics,
        auto_commit: bool = True,
    ) -> ExperimentMetrics:
        """Record a measured experiment result snapshot.

        Enforces:
        - Experiment must exist (foreign-key / association validation).
        - Preserves evidence category (FACT / INFERENCE / HYPOTHESIS / PREDICTION).
        - Preserves source reference / audit trail.
        - Strictly isolated from the financial ledger (zero capital transactions created).
        - No automated decisions (SCALE / ITERATE / KILL / HOLD).
        """
        exp_orm = session.get(ExperimentORM, metrics.experiment_id)
        if exp_orm is None:
            raise ValueError(f"Experiment '{metrics.experiment_id}' does not exist.")

        # Compute deterministic profit_loss if revenue is known
        updated_profit_loss = metrics.profit_loss
        if metrics.revenue is not None and metrics.cost is not None:
            expected_pl = metrics.revenue - metrics.cost
            if updated_profit_loss is None or (
                updated_profit_loss == Decimal("0")
                and (metrics.revenue != Decimal("0") or metrics.cost != Decimal("0"))
            ):
                updated_profit_loss = expected_pl
        elif metrics.revenue is None:
            updated_profit_loss = None

        # Deterministic conversion_rate computation if visitors/clicks and conversions provided and rate is None
        computed_cr = metrics.conversion_rate
        if computed_cr is None and metrics.conversions is not None:
            base_count = metrics.visitors if metrics.visitors is not None else metrics.clicks
            if base_count and base_count > 0:
                computed_cr = round(metrics.conversions / base_count, 4)

        # Deterministic ROI / ROAS if cost > 0, values are None, and financial inputs are known
        computed_roi = metrics.roi
        if computed_roi is None and metrics.cost > Decimal("0") and updated_profit_loss is not None:
            computed_roi = round(float(updated_profit_loss / metrics.cost), 4)

        computed_roas = metrics.roas
        if (
            computed_roas is None
            and metrics.cost > Decimal("0")
            and metrics.revenue is not None
            and metrics.revenue > Decimal("0")
        ):
            computed_roas = round(metrics.revenue / metrics.cost, 2)

        final_metrics = ExperimentMetrics(
            id=metrics.id,
            experiment_id=metrics.experiment_id,
            evidence_type=metrics.evidence_type,
            source_reference=metrics.source_reference,
            impressions=metrics.impressions,
            clicks=metrics.clicks,
            visitors=metrics.visitors,
            conversions=metrics.conversions,
            conversion_rate=computed_cr,
            revenue=metrics.revenue,
            cost=metrics.cost,
            profit_loss=updated_profit_loss,
            roas=computed_roas,
            roi=computed_roi,
            retention_notes=metrics.retention_notes,
            recorded_at=metrics.recorded_at,
        )

        repo = MetricsRepository(session, auto_commit=auto_commit)
        return repo.create(final_metrics)

    @classmethod
    def get_measurement(
        cls,
        session: Session,
        metrics_id: UUID,
    ) -> ExperimentMetrics | None:
        """Retrieve a specific measurement record by ID."""
        repo = MetricsRepository(session, auto_commit=False)
        return repo.get(metrics_id)

    @classmethod
    def list_measurements_for_experiment(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> list[ExperimentMetrics]:
        """Retrieve all recorded measurements for an experiment in chronological order."""
        repo = MetricsRepository(session, auto_commit=False)
        return repo.list_for_experiment(experiment_id)

    @classmethod
    def get_latest_measurement(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> ExperimentMetrics | None:
        """Retrieve the most recent measurement snapshot for an experiment."""
        repo = MetricsRepository(session, auto_commit=False)
        return repo.get_latest_for_experiment(experiment_id)
