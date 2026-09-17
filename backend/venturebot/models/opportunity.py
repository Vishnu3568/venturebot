"""Opportunity — a potentially monetizable opportunity discovered by VentureBot."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class OpportunityCategory(str, Enum):
    CONTENT = "content"
    FREELANCE = "freelance"
    PRODUCT = "product"
    SERVICE = "service"
    AFFILIATE = "affiliate"
    RESELLING = "reselling"
    OTHER = "other"


class OpportunityStatus(str, Enum):
    DISCOVERED = "discovered"      # just logged
    UNDER_REVIEW = "under_review"  # being evaluated
    APPROVED = "approved"          # cleared for experiment
    REJECTED = "rejected"          # ruled out
    ARCHIVED = "archived"          # experiment done, no longer active


class Opportunity(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    description: str
    category: OpportunityCategory
    status: OpportunityStatus = OpportunityStatus.DISCOVERED

    # Evidence
    source: str = ""                    # URL, person, tool that surfaced this
    evidence_notes: str = ""            # raw notes / references

    # Market signals (all optional — fill in what's known)
    audience: str = ""                  # who would pay / engage
    trend_strength: str = ""            # e.g. "rising", "stable", "declining"
    growth_indicators: str = ""         # data points suggesting growth
    competition_level: str = ""         # e.g. "low", "medium", "high", or free text

    # Feasibility
    monetization_notes: str = ""        # how money could be made
    production_difficulty: str = ""     # effort to create the product/service
    distribution_difficulty: str = ""   # effort to reach the audience
    automation_potential: str = ""      # how automatable is delivery
    platform_dependency: str = ""       # which platforms this relies on
    regulatory_notes: str = ""          # legal / risk considerations

    # Economics (estimates only — no formula applied yet)
    estimated_revenue_min: Decimal = Decimal("0")   # ₹
    estimated_revenue_max: Decimal = Decimal("0")   # ₹
    estimated_cost_min: Decimal = Decimal("0")       # ₹
    estimated_cost_max: Decimal = Decimal("0")       # ₹

    # Confidence (0.0–1.0, human-assigned, no algorithm yet)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
