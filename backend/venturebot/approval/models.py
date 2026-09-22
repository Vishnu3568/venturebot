"""Approval data contracts for VentureBot Experiment review and capital allocation."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from venturebot.models.capital import CapitalTransaction
from venturebot.models.decision import Decision
from venturebot.models.experiment import Experiment


class ApprovalRequest(BaseModel):
    """Structured request for human or explicit review/approval of an Experiment."""
    experiment_id: UUID
    reason: str = Field(min_length=1)
    allocated_budget: Decimal | None = Field(default=None, ge=Decimal("0.00"))
    max_allowed_spend: Decimal | None = Field(default=None, ge=Decimal("0.00"))


class ApprovalResult(BaseModel):
    """Result of an experiment approval or rejection attempt."""
    is_approved: bool
    experiment_id: UUID
    experiment: Experiment | None = None
    decision: Decision | None = None
    allocation_transaction: CapitalTransaction | None = None
    rejection_reason: str | None = None
