"""Evaluation schemas and result models for Opportunity evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class EvaluationStatus(str, Enum):
    """Deterministic readiness classification for an Opportunity."""
    READY_FOR_EXPERIMENT_DESIGN = "ready_for_experiment_design"
    NEEDS_EVIDENCE = "needs_evidence"
    INSUFFICIENT_ECONOMICS = "insufficient_economics"
    EXCEEDS_CAPITAL_LIMIT = "exceeds_capital_limit"
    INCOMPLETE = "incomplete"


class OpportunityEvaluationResult(BaseModel):
    """Structured, deterministic evaluation assessment of an Opportunity.
    
    Per VENTUREBOT_ARCHITECTURE.md Section 8, no arbitrary scoring formulas or
    speculative point weights are applied. Evaluation focuses strictly on evidence
    completeness, financial boundaries, feasibility, and factual dimensions.
    """
    opportunity_id: UUID
    status: EvaluationStatus

    # Evidence & completeness metrics
    has_source_evidence: bool
    provided_dimensions_count: int
    total_dimensions_count: int
    completeness_ratio: float = Field(ge=0.0, le=1.0)
    missing_dimensions: list[str] = Field(default_factory=list)

    # Economics analysis
    has_economic_estimates: bool
    estimated_margin_min: Decimal = Decimal("0.00")
    estimated_margin_max: Decimal = Decimal("0.00")
    exceeds_starting_capital_risk: bool = False

    # Factual risk flags
    risk_flags: list[str] = Field(default_factory=list)

    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
