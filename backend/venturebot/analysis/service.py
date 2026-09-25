"""Experiment Performance Analysis Service for VentureBot (Step 12).

Provides deterministic factual synthesis of recorded experiment measurements.
Strictly read-only: does not modify the capital ledger or create decisions.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
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


def _normalize_dt(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone for safe comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_window_dates(source_ref: str) -> tuple[datetime, datetime] | None:
    """Parse start and stop datetimes from a canonical Meta campaign reporting-window string."""
    if not source_ref or not source_ref.startswith("meta:insights:campaign:"):
        return None
    parts = source_ref.split(":")
    if len(parts) >= 6:
        try:
            start_date = date.fromisoformat(parts[-2])
            stop_date = date.fromisoformat(parts[-1])
            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
            stop_dt = datetime.combine(stop_date, datetime.max.time(), tzinfo=timezone.utc)
            return (start_dt, stop_dt)
        except (ValueError, TypeError):
            return None
    return None


class ExperimentAnalysisService:
    """Read-only deterministic service for analyzing recorded experiment metrics."""

    @classmethod
    def _resolve_checkpoints(
        cls,
        measurements: list[ExperimentMetrics],
    ) -> list[ExperimentMetrics]:
        """Resolve measurement observations into chronological performance checkpoints.

        Locked Semantics (Section 24.7 of Architecture):
        - RESTATEMENT != NEW PERFORMANCE PERIOD.
        - Measurements that do NOT have a reporting-window source_reference (empty or whitespace)
          retain their existing behavior as distinct individual checkpoints.
        - Measurements that share a logical reporting-window identity (source_reference)
          are grouped together.
        - Within each logical reporting window, the latest observation by recorded_at DESC
          is selected as the authoritative checkpoint observation.
        - Resolved checkpoints are ordered chronologically by reporting-window delivery
          date semantics if available (e.g. Meta start/stop dates), or by the window's
          earliest observation timestamp and appearance order.
        """
        if not measurements:
            return []

        appearance_index: dict[UUID, int] = {m.id: idx for idx, m in enumerate(measurements)}

        # Group observations by logical reporting window
        # Unwindowed observations (empty or whitespace source_reference) each get their own unique group.
        grouped: dict[str | tuple[str, UUID], list[ExperimentMetrics]] = {}
        for m in measurements:
            ref = m.source_reference.strip() if m.source_reference else ""
            if ref:
                key: str | tuple[str, UUID] = ref
            else:
                key = ("__unwindowed__", m.id)

            if key not in grouped:
                grouped[key] = []
            grouped[key].append(m)

        # For each group, select the latest observation by recorded_at DESC (and appearance order)
        # and compute the chronological sort key for the window.
        resolved_windows: list[tuple[tuple[datetime, datetime, int], ExperimentMetrics]] = []
        for key, obs_list in grouped.items():
            latest_obs = max(
                obs_list,
                key=lambda m: (_normalize_dt(m.recorded_at), appearance_index.get(m.id, 0)),
            )

            first_idx = min(appearance_index.get(m.id, 0) for m in obs_list)
            if isinstance(key, str):
                parsed = _parse_window_dates(key)
                if parsed is not None:
                    sort_key = (parsed[0], parsed[1], first_idx)
                else:
                    earliest_dt = min(_normalize_dt(m.recorded_at) for m in obs_list)
                    sort_key = (earliest_dt, earliest_dt, first_idx)
            else:
                earliest_dt = min(_normalize_dt(m.recorded_at) for m in obs_list)
                sort_key = (earliest_dt, earliest_dt, first_idx)

            resolved_windows.append((sort_key, latest_obs))

        resolved_windows.sort(key=lambda item: item[0])
        return [item[1] for item in resolved_windows]

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

        checkpoints = cls._resolve_checkpoints(measurements)
        latest: ExperimentMetrics = checkpoints[-1]

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

        # 4. Measure-to-measure changes between checkpoints if 2+ distinct checkpoints exist
        changes: list[MetricDelta] = []
        if len(checkpoints) >= 2:
            prev: ExperimentMetrics = checkpoints[-2]
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
                            source_reference=f"Comparison between checkpoint {len(checkpoints)-1} and {len(checkpoints)}",
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
