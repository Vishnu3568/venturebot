"""External execution models for VentureBot (Step 39)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ExternalExecutionStatus(str, Enum):
    """Lifecycle status of an external deployment."""

    PENDING = "pending"
    PARTIAL_CAMPAIGN = "partial_campaign"
    PARTIAL_ADSET = "partial_adset"
    PARTIAL_CREATIVE = "partial_creative"
    DEPLOYED = "deployed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ExternalExecution(BaseModel):
    """Canonical data contract for the current external deployment of an Experiment."""

    id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID
    channel: str = "meta"
    external_account_id: str
    campaign_id: str | None = None
    adset_id: str | None = None
    creative_id: str | None = None
    ad_id: str | None = None
    image_hash: str | None = None
    status: ExternalExecutionStatus = ExternalExecutionStatus.PENDING
    last_error: str | None = None
    dispatched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
