"""Experiment Performance Analysis Service for VentureBot (Step 12).

Provides deterministic factual synthesis of recorded experiment measurements.
Strictly read-only: does not modify the capital ledger or create decisions.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.analysis.models import (
    ExperimentPerformanceAnalysis,
    MetricDelta,
    PerformanceObservation,
)
from venturebot.database.models import ExperimentORM
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.metrics import ExperimentMetrics

TRACKABLE_METRICS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "visitors",
    "conversions",
    "conversion_rate",
    "revenue",
    "cost",
    "profit_loss",
    "roas",
    "roi",
    "retention_notes",
)

NUMERIC_COMPARE_FIELDS: tuple[str, ...] = (
    "revenue",
    "cost",
    "profit_loss",
    "visitors",
    "conversions",
    "clicks",
    "impressions",
    "conversion_rate",
    "roi",
    "roas",
)


class ExperimentAnalysisService:
    """Read-only deterministic service for analyzing recorded experiment metrics."""

    @classmethod
    def analyze(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> ExperimentPerformanceAnalysis:
        """Produce a structured, factual performance analysis for an experiment.

        Enforces:
        - Experiment must exist in database.
        - Strictly read-only: zero ledger transactions created, zero balance mutations.
        - Strictly factual / inferential: does not invent arbitrary thresholds, scores, or decisions.
        - Explicitly distinguishes available vs missing metrics (no fake zero substitutions).
        """
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            raise ValueError(f"Experiment '{experiment_id}' does not exist.")

        measurements = ExperimentMeasurementService.list_measurements_for_experiment(
            session, experiment_id
        )

        if not measurements:
            return ExperimentPerformanceAnalysis(
                experiment_id=experiment_id,
                opportunity_id=exp_orm.opportunity_id,
                total_measurements=0,
                latest_measurement=None,
                available_metrics=[],
                missing_metrics=list(TRACKABLE_METRICS),
                financial_summary={
                    "revenue": None,
                    "cost": None,
                    "profit_loss": None,
                    "roi": None,
                    "roas": None,
                },
                changes_from_previous=[],
                observations=[
                    PerformanceObservation(
                        category=EvidenceCategory.FACT,
                        statement="No measurements have been recorded for this experiment.",
                    )
                ],
            )

        latest: ExperimentMetrics = measurements[-1]

        # 1. Determine available vs missing metrics on latest snapshot
        available: list[str] = []
        missing: list[str] = []
        for field in TRACKABLE_METRICS:
            val = getattr(latest, field, None)
            if val is None or (field == "retention_notes" and not str(val).strip()):
                missing.append(field)
            else:
                available.append(field)

        # 2. Extract deterministic financial summary
        financial_summary: dict[str, Decimal | float | None] = {
            "revenue": latest.revenue,
            "cost": latest.cost,
            "profit_loss": latest.profit_loss,
            "roi": latest.roi,
            "roas": latest.roas,
        }

        # 3. Formulate factual observations directly grounded in the latest record
        observations: list[PerformanceObservation] = []
        src_ref = latest.source_reference.strip() if latest.source_reference else "direct recording"

        if latest.revenue is not None:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Latest recorded revenue is ₹{latest.revenue:.2f}.",
                    source_reference=src_ref,
                )
            )
        observations.append(
            PerformanceObservation(
                category=EvidenceCategory.FACT,
                statement=f"Latest recorded cost is ₹{latest.cost:.2f}.",
                source_reference=src_ref,
            )
        )
        if latest.profit_loss is not None:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Latest recorded profit/loss is ₹{latest.profit_loss:.2f}.",
                    source_reference=src_ref,
                )
            )

        if latest.conversion_rate is not None:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Latest recorded conversion rate is {latest.conversion_rate:.2%}.",
                    source_reference=src_ref,
                )
            )

        if latest.visitors is not None:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Latest recorded visitors count is {latest.visitors}.",
                    source_reference=src_ref,
                )
            )

        if latest.conversions is not None:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Latest recorded conversions count is {latest.conversions}.",
                    source_reference=src_ref,
                )
            )

        if latest.retention_notes and latest.retention_notes.strip():
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Recorded retention notes: {latest.retention_notes.strip()}",
                    source_reference=src_ref,
                )
            )

        if missing:
            observations.append(
                PerformanceObservation(
                    category=EvidenceCategory.FACT,
                    statement=f"Missing/unmeasured metrics in latest snapshot: {', '.join(missing)}.",
                    source_reference=src_ref,
                )
            )

        # 4. Measure-to-measure changes if 2+ measurements exist
        changes: list[MetricDelta] = []
        if len(measurements) >= 2:
            prev: ExperimentMetrics = measurements[-2]
            for field in NUMERIC_COMPARE_FIELDS:
                prev_val = getattr(prev, field, None)
                latest_val = getattr(latest, field, None)
                if prev_val is not None and latest_val is not None:
                    delta = latest_val - prev_val
                    pct_change: float | None = None
                    if prev_val != 0:
                        try:
                            pct_change = round(float(delta / prev_val) * 100, 2)
                        except (ZeroDivisionError, OverflowError):
                            pct_change = None

                    # Format clean currency or rate statement
                    if isinstance(delta, Decimal):
                        stmt = (
                            f"{field.replace('_', ' ').title()} changed from ₹{prev_val:.2f} "
                            f"to ₹{latest_val:.2f} (delta: {'+' if delta > 0 else ''}₹{delta:.2f})."
                        )
                    else:
                        stmt = (
                            f"{field.replace('_', ' ').title()} changed from {prev_val} "
                            f"to {latest_val} (delta: {'+' if delta > 0 else ''}{delta})."
                        )

                    metric_delta = MetricDelta(
                        metric_name=field,
                        previous_value=prev_val,
                        latest_value=latest_val,
                        absolute_change=delta,
                        percentage_change=pct_change,
                        statement=stmt,
                    )
                    changes.append(metric_delta)

                    # Add an INFERENCE observation grounded in the measured facts
                    observations.append(
                        PerformanceObservation(
                            category=EvidenceCategory.INFERENCE,
                            statement=stmt,
                            source_reference=f"Comparison between snapshot {len(measurements)-1} and {len(measurements)}",
                        )
                    )

        return ExperimentPerformanceAnalysis(
            experiment_id=experiment_id,
            opportunity_id=exp_orm.opportunity_id,
            total_measurements=len(measurements),
            latest_measurement=latest,
            available_metrics=available,
            missing_metrics=missing,
            financial_summary=financial_summary,
            changes_from_previous=changes,
            observations=observations,
        )
