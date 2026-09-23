"""Performance Analysis data contracts for VentureBot (Step 12).

Structures factual analysis and deterministic observations derived from
already-recorded ExperimentMetrics.
Does NOT make decisions (no automatic SCALE / ITERATE / KILL / HOLD).
Does NOT invent arbitrary scoring formulas, weights, or business thresholds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from venturebot.models.evidence import EvidenceCategory
from venturebot.models.metrics import ExperimentMetrics


class MetricDelta(BaseModel):
    """Factual measurement-to-measurement change between two records."""

    metric_name: str
    previous_value: Decimal | float | int | str | None = None
    latest_value: Decimal | float | int | str | None = None
    absolute_change: Decimal | float | int | None = None
    percentage_change: float | None = None
    statement: str


class PerformanceObservation(BaseModel):
    """Individual factual statement or derived inference grounded in evidence."""

    category: EvidenceCategory  # FACT (directly recorded) or INFERENCE (derived from facts)
    statement: str
    source_reference: str = ""


class ExperimentPerformanceAnalysis(BaseModel):
    """Structured performance analysis synthesized from recorded experiment measurements.

    Provides a clean, deterministic summary of what actually happened during an experiment:
    - What metrics are available vs missing
    - Current financial metrics (revenue, cost, profit/loss, ROI, ROAS)
    - Measurement deltas over time
    - Factual observations and inferences

    This contract is strictly informational. It contains no decision recommendation
    and triggers zero ledger or state side-effects.
    """

    experiment_id: UUID
    opportunity_id: UUID | None = None
    total_measurements: int
    latest_measurement: ExperimentMetrics | None = None

    available_metrics: list[str] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)

    financial_summary: dict[str, Decimal | float | None] = Field(default_factory=dict)
    changes_from_previous: list[MetricDelta] = Field(default_factory=list)
    observations: list[PerformanceObservation] = Field(default_factory=list)

    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
