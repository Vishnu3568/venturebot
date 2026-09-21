"""Repository for ExperimentMetrics persistence and queries."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentMetricsORM
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.metrics import ExperimentMetrics


class MetricsRepository:
    """Repository managing ExperimentMetrics persistence in the SQLite database."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def create(self, metrics: ExperimentMetrics) -> ExperimentMetrics:
        """Persist a new ExperimentMetrics snapshot."""
        evidence_val = (
            metrics.evidence_type.value
            if hasattr(metrics.evidence_type, "value")
            else str(metrics.evidence_type)
        )
        orm = ExperimentMetricsORM(
            id=metrics.id,
            experiment_id=metrics.experiment_id,
            evidence_type=evidence_val,
            source_reference=metrics.source_reference,
            impressions=metrics.impressions,
            clicks=metrics.clicks,
            visitors=metrics.visitors,
            conversions=metrics.conversions,
            conversion_rate=metrics.conversion_rate,
            revenue=metrics.revenue,
            cost=metrics.cost,
            profit_loss=metrics.profit_loss,
            roas=metrics.roas,
            roi=metrics.roi,
            retention_notes=metrics.retention_notes,
            recorded_at=metrics.recorded_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    def get(self, metrics_id: UUID) -> ExperimentMetrics | None:
        """Retrieve a metrics record by unique ID."""
        orm = self.session.get(ExperimentMetricsORM, metrics_id)
        return self._to_pydantic(orm) if orm is not None else None

    def list_for_experiment(self, experiment_id: UUID) -> list[ExperimentMetrics]:
        """List all metrics recordings for a given experiment in chronological order."""
        stmt = (
            select(ExperimentMetricsORM)
            .where(ExperimentMetricsORM.experiment_id == experiment_id)
            .order_by(ExperimentMetricsORM.recorded_at.asc(), ExperimentMetricsORM.id.asc())
        )
        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def get_latest_for_experiment(self, experiment_id: UUID) -> ExperimentMetrics | None:
        """Retrieve the most recently recorded metrics for an experiment."""
        stmt = (
            select(ExperimentMetricsORM)
            .where(ExperimentMetricsORM.experiment_id == experiment_id)
            .order_by(ExperimentMetricsORM.recorded_at.desc(), ExperimentMetricsORM.id.desc())
            .limit(1)
        )
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    @staticmethod
    def _to_pydantic(orm: ExperimentMetricsORM) -> ExperimentMetrics:
        """Map ExperimentMetricsORM to canonical Pydantic model."""
        recorded_at = orm.recorded_at
        if recorded_at is not None and recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)

        raw_evidence = getattr(orm, "evidence_type", "fact") or "fact"
        try:
            evidence_enum = EvidenceCategory(raw_evidence)
        except ValueError:
            evidence_enum = EvidenceCategory.FACT

        return ExperimentMetrics(
            id=orm.id,
            experiment_id=orm.experiment_id,
            evidence_type=evidence_enum,
            source_reference=getattr(orm, "source_reference", "") or "",
            impressions=orm.impressions,
            clicks=orm.clicks,
            visitors=orm.visitors,
            conversions=orm.conversions,
            conversion_rate=orm.conversion_rate,
            revenue=(
                orm.revenue
                if orm.revenue is None or isinstance(orm.revenue, Decimal)
                else Decimal(str(orm.revenue))
            ),
            cost=(
                orm.cost
                if isinstance(orm.cost, Decimal)
                else Decimal(str(orm.cost or 0))
            ),
            profit_loss=(
                orm.profit_loss
                if orm.profit_loss is None or isinstance(orm.profit_loss, Decimal)
                else Decimal(str(orm.profit_loss))
            ),
            roas=(
                orm.roas
                if orm.roas is None or isinstance(orm.roas, Decimal)
                else Decimal(str(orm.roas))
            ),
            roi=orm.roi,
            retention_notes=orm.retention_notes or "",
            recorded_at=recorded_at,
        )
