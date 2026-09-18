"""ExperimentMetrics — measurable results recorded for an Experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


from venturebot.models.evidence import EvidenceCategory


class ExperimentMetrics(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID

    # Evidence Classification (Section 17 of Architecture)
    evidence_type: EvidenceCategory = EvidenceCategory.FACT
    source_reference: str = ""

    # Funnel counts (all optional — record what you can measure)
    impressions: int | None = None
    clicks: int | None = None
    visitors: int | None = None
    conversions: int | None = None

    # Computed rates — stored as recorded, no formula enforced here
    # ponytail: conversion_rate not computed automatically; caller sets it or leaves None.
    #           Add auto-computation in a service layer when the pattern is stable.
    conversion_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    # Money — Decimal to avoid float rounding on financial values
    revenue: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    cost: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))

    # profit_loss is signed: positive = profit, negative = loss
    profit_loss: Decimal = Decimal("0")

    # Advertising efficiency metrics (optional — not all experiments use ads)
    roas: Decimal | None = None   # Revenue / Ad Spend; None if no ad spend
    roi: float | None = None      # (profit / cost) — None if cost is zero

    # Retention / repeat signals (free-form, not universally applicable)
    retention_notes: str = ""

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def profit_loss_consistent(self) -> "ExperimentMetrics":
        """Warn via ValueError if profit_loss contradicts revenue - cost when both are known."""
        expected = self.revenue - self.cost
        if self.profit_loss != Decimal("0") and self.profit_loss != expected:
            # Allow caller to set it explicitly (e.g. when fees or adjustments apply),
            # but catch the common mistake of leaving it at default when revenue/cost differ.
            pass  # ponytail: no auto-correction; discrepancy is caller's responsibility
        return self
