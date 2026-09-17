"""Experiment — a controlled test derived from an Opportunity."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class ExperimentStatus(str, Enum):
    DRAFT = "draft"           # being designed
    APPROVED = "approved"     # approved to run, not yet started
    RUNNING = "running"       # active
    PAUSED = "paused"         # temporarily stopped
    COMPLETED = "completed"   # finished normally
    KILLED = "killed"         # stopped early (failure/decision)


class Channel(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TWITTER_X = "twitter_x"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"
    REDDIT = "reddit"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    WEBSITE = "website"
    MARKETPLACE = "marketplace"
    OTHER = "other"


class MonetizationMethod(str, Enum):
    DIRECT_SALE = "direct_sale"
    SUBSCRIPTION = "subscription"
    AFFILIATE = "affiliate"
    LEAD_GEN = "lead_gen"
    SPONSORSHIP = "sponsorship"
    FREELANCE = "freelance"
    ADS_REVENUE = "ads_revenue"
    OTHER = "other"


class Experiment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    opportunity_id: UUID                    # must link to an Opportunity

    hypothesis: str                         # "We believe that X will result in Y"
    objective: str                          # measurable goal
    channel: Channel
    monetization_method: MonetizationMethod

    # Budget — these two MUST be kept distinct (core constraint)
    allocated_budget: Decimal = Field(ge=Decimal("0"))   # ₹ approved for this experiment
    max_allowed_spend: Decimal = Field(ge=Decimal("0"))  # ₹ hard ceiling; must not exceed
    actual_spend: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))  # ₹ spent so far

    # Criteria
    success_criteria: str                   # what "worked" looks like
    failure_criteria: str                   # what triggers a kill

    # Timeline
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None

    status: ExperimentStatus = ExperimentStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def spend_within_ceiling(self) -> "Experiment":
        if self.actual_spend > self.max_allowed_spend:
            raise ValueError(
                f"actual_spend ({self.actual_spend}) exceeds max_allowed_spend ({self.max_allowed_spend})"
            )
        if self.allocated_budget > self.max_allowed_spend:
            raise ValueError(
                f"allocated_budget ({self.allocated_budget}) exceeds max_allowed_spend ({self.max_allowed_spend})"
            )
        return self
