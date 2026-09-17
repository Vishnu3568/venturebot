"""CapitalTransaction — auditable record of every money movement in VentureBot's pool."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TransactionType(str, Enum):
    INITIAL_DEPOSIT = "initial_deposit"   # starting capital added
    EXPERIMENT_ALLOCATION = "experiment_allocation"  # budget reserved for an experiment
    EXPERIMENT_SPEND = "experiment_spend"             # actual spend recorded
    EXPERIMENT_REFUND = "experiment_refund"           # unused allocation returned
    REVENUE = "revenue"                               # money earned
    WITHDRAWAL = "withdrawal"                         # capital taken out by owner
    ADJUSTMENT = "adjustment"                         # manual correction with reason


class CapitalTransaction(BaseModel):
    id: UUID = Field(default_factory=uuid4)

    # experiment_id is optional — some transactions are not experiment-linked
    experiment_id: UUID | None = None

    transaction_type: TransactionType

    # amount is always positive; direction is implied by transaction_type
    # ponytail: signed amounts deferred — positive-only + type is simpler to audit
    amount: Decimal = Field(ge=Decimal("0.01"))   # ₹; must be > 0

    description: str   # required — every transaction must explain itself

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
