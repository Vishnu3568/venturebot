"""Experiment Learning data contracts for VentureBot (Step 13).

Provides structured, append-only records of explicitly supplied retrospective
experiment learning.
Does NOT automatically generate learning.
Does NOT add arbitrary scores, categories, or unsolicited taxonomy tags.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class ExperimentLearning(BaseModel):
    """Structured, append-only record of explicitly recorded experiment learning."""

    id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID
    opportunity_id: UUID
    decision_id: UUID | None = None

    summary: str
    what_worked: list[str] = Field(default_factory=list)
    what_failed: list[str] = Field(default_factory=list)
    key_learnings: list[str] = Field(default_factory=list)
    future_hypotheses: list[str] = Field(default_factory=list)

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("summary")
    @classmethod
    def summary_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Learning summary must not be empty.")
        return clean

    @field_validator("key_learnings")
    @classmethod
    def key_learnings_must_have_at_least_one_entry(cls, v: list[str]) -> list[str]:
        cleaned = [item.strip() for item in v if item and item.strip()]
        if not cleaned:
            raise ValueError("At least one non-empty key learning is required.")
        return cleaned

    @field_validator("what_worked", "what_failed", "future_hypotheses")
    @classmethod
    def clean_string_lists(cls, v: list[str]) -> list[str]:
        return [item.strip() for item in v if item and item.strip()]
