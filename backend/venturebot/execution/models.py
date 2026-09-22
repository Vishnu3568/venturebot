"""Execution models for VentureBot internal experiment execution lifecycle."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from venturebot.models.capital import CapitalTransaction
from venturebot.models.decision import Decision
from venturebot.models.experiment import Experiment


class ExecutionResult(BaseModel):
    """Result of an experiment execution lifecycle action or spend recording."""
    is_successful: bool
    experiment_id: UUID
    experiment: Experiment | None = None
    decision: Decision | None = None
    transaction: CapitalTransaction | None = None
    rejection_reason: str | None = None
