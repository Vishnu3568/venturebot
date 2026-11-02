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
    DEPLOYED_DORMANT = "deployed_dormant"
    DEPLOYED = "deployed"
    ACTIVATED = "activated"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ExternalExecutionEventType(str, Enum):
    """Event types for immutable external execution audit logs."""

    DEPLOYMENT_ATTEMPT = "deployment_attempt"
    DEPLOYMENT_FAILED = "deployment_failed"
    RESOURCE_CREATED = "resource_created"
    RESOURCE_ADOPTED = "resource_adopted"
    RESOURCE_SUPERSEDED = "resource_superseded"
    SCHEDULE_LOCKED = "schedule_locked"
    READINESS_CHECKED = "readiness_checked"
    ACTIVATION_AUTHORIZED = "activation_authorized"
    ACTIVATED = "activated"
    DELIVERY_VERIFIED = "delivery_verified"
    EMERGENCY_STOP = "emergency_stop"


class ExternalExecutionEvent(BaseModel):
    """Canonical immutable audit log record for external execution actions and transitions."""

    id: UUID = Field(default_factory=uuid4)
    execution_id: UUID
    event_type: str
    tier: str
    old_resource_id: str | None = None
    new_resource_id: str | None = None
    reason: str
    api_request_summary: str | None = None
    api_response_summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
