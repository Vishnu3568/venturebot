"""Proposal data contracts for VentureBot Opportunity -> Experiment planning."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from venturebot.evaluation.models import EvaluationStatus
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod


class ExperimentProposal(BaseModel):
    """Structured, reviewable specification for a potential experiment.
    
    This is a pre-execution specification layer. Generating or holding a proposal
    does NOT disburse money, create ledger transactions, or execute campaigns.
    """
    id: UUID = Field(default_factory=uuid4)
    opportunity_id: UUID
    opportunity_title: str

    # Core experiment design
    hypothesis: str
    test_description: str
    channel: Channel
    monetization_method: MonetizationMethod

    # Budget parameters (proposals only - not actual spend)
    proposed_budget: Decimal = Field(ge=Decimal("0.00"))
    max_allowed_spend: Decimal = Field(ge=Decimal("0.00"))
    estimated_revenue_target: Decimal = Field(ge=Decimal("0.00"))
    estimated_margin: Decimal

    timeline_days: int | None = Field(default=None, ge=1, le=90)

    # Decision criteria
    success_criteria: str
    failure_criteria: str
    key_metrics_to_track: list[str] = Field(default_factory=list)
    evidence_for_decision: str = ""
    risks_and_constraints: list[str] = Field(default_factory=list)
    proposal_rationale: str = ""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_experiment(self) -> Experiment:
        """Convert approved proposal into a canonical draft Experiment model.
        
        Initializes actual_spend at Decimal('0.00') and status at ExperimentStatus.DRAFT.
        """
        return Experiment(
            id=self.id,
            opportunity_id=self.opportunity_id,
            hypothesis=self.hypothesis,
            objective=self.test_description,
            channel=self.channel,
            monetization_method=self.monetization_method,
            allocated_budget=self.proposed_budget,
            max_allowed_spend=self.max_allowed_spend,
            actual_spend=Decimal("0.00"),
            success_criteria=self.success_criteria,
            failure_criteria=self.failure_criteria,
            status=ExperimentStatus.DRAFT,
        )


class ProposalGenerationResult(BaseModel):
    """Result wrapper indicating proposal eligibility and containing generated proposal."""
    is_eligible: bool
    evaluation_status: EvaluationStatus
    proposal: ExperimentProposal | None = None
    rejection_reason: str | None = None
