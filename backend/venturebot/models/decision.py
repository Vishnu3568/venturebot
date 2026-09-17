"""Decision — a recorded human or system decision about an Opportunity or Experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class DecisionOutcome(str, Enum):
    KILL = "kill"       # stop, do not revisit
    ITERATE = "iterate" # change something and try again
    SCALE = "scale"     # increase budget / reach
    HOLD = "hold"       # pause, gather more data before acting


class Decision(BaseModel):
    id: UUID = Field(default_factory=uuid4)

    # Exactly one of these should be set; both optional to allow flexibility
    # ponytail: mutual-exclusion not enforced here — add a validator when the
    #           rule is confirmed (opportunity-only vs experiment-only decisions).
    opportunity_id: UUID | None = None
    experiment_id: UUID | None = None

    outcome: DecisionOutcome
    reason: str            # human-readable rationale (required)
    evidence_summary: str = ""   # metrics / data that supported this decision

    # Confidence (0.0–1.0, human-assigned; no algorithm yet)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
