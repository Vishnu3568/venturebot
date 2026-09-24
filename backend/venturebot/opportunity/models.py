"""Opportunity Intelligence data contracts for VentureBot (Step 14).

Provides structured, read-only intelligence contexts synthesizing existing data
for an Opportunity without creating duplicate database entities.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from venturebot.evaluation.models import OpportunityEvaluationResult
from venturebot.learning.models import ExperimentLearning
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, ExperimentStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


class ResearchEvidenceItem(BaseModel):
    """Discrete research evidence item supporting an opportunity."""

    statement: str
    category: EvidenceCategory
    source_reference: str
    observation_date: date | None = None
    metric_value: Decimal | None = None

    @field_validator("statement")
    @classmethod
    def statement_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Evidence statement must not be empty or whitespace.")
        return clean

    @field_validator("source_reference")
    @classmethod
    def source_reference_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Evidence source_reference must not be empty or whitespace.")
        return clean


class TrendStatus(str, Enum):
    """Directional trend status supported strictly by empirical observations."""

    INSUFFICIENT_DATA = "insufficient_data"
    INCREASING = "increasing"
    DECREASING = "decreasing"
    STABLE = "stable"
    NO_DIRECTIONAL_CHANGE = "no_directional_change"


class ResearchTrendObservation(BaseModel):
    """Transient analytical result representing directional trend across dated observations.

    Strictly separates:
    - FACT: original dated empirical research observations and source references
    - ANALYSIS: objective calculation of directional movement over time
    - STATUS: descriptive trend status (INSUFFICIENT_DATA, INCREASING, DECREASING, STABLE, NO_DIRECTIONAL_CHANGE)

    Does NOT contain commercial hypotheses, market demand claims, scores, or rankings.
    Does NOT write to database, modify ledger, or create opportunities/experiments.
    """

    topic: str
    status: TrendStatus
    fact_summary: str
    analysis: str
    evidence_items: list[ResearchEvidenceItem] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Trend observation topic must not be empty or whitespace.")
        return clean

    @field_validator("fact_summary", "analysis")
    @classmethod
    def text_fields_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Trend observation fact_summary and analysis must not be empty.")
        return clean

    @property
    def evidence_count(self) -> int:
        """Return the number of supporting evidence items."""
        return len(self.evidence_items)


class ResearchCollectionResult(BaseModel):
    """Transient container representing a completed research collection run from an external source.

    Holds discrete research observations for an explicit date without creating domain
    opportunities or triggering database writes.
    """

    source_id: str
    collection_date: date
    evidence_items: list[ResearchEvidenceItem] = Field(default_factory=list)

    @field_validator("source_id")
    @classmethod
    def source_id_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("source_id must not be empty or whitespace.")
        return clean

    @property
    def count(self) -> int:
        """Return the number of collected evidence items."""
        return len(self.evidence_items)


class OpportunityCandidate(BaseModel):
    """Transient structured candidate hypothesis derived from research evidence.

    Clearly separates:
    - FACT: original empirical research evidence items
    - OBSERVATION: observed external signal
    - INFERENCE: logical deduction regarding attention
    - HYPOTHESIS: testable premise of potential business viability
    - UNKNOWNS: explicitly documented unvalidated dimensions

    Does NOT write to database, score opportunities, or create experiments.
    """

    id: UUID = Field(default_factory=uuid4)
    title: str
    topic: str
    observation: str
    inference: str
    hypothesis: str
    unknowns: list[str] = Field(default_factory=list)
    evidence_items: list[ResearchEvidenceItem] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)
    trend_status: TrendStatus | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("title")
    @classmethod
    def title_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Candidate title must not be empty or whitespace.")
        return clean

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Candidate topic must not be empty or whitespace.")
        return clean

    @field_validator("observation", "inference", "hypothesis")
    @classmethod
    def text_fields_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Candidate observation, inference, and hypothesis must not be empty.")
        return clean

    @property
    def evidence_count(self) -> int:
        """Return the number of supporting evidence items."""
        return len(self.evidence_items)

    def to_ingestion_payload(
        self,
        opportunity: Opportunity,
    ) -> OpportunityIngestionPayload:
        """Package this candidate's evidence with a human-specified Opportunity into an ingestion payload."""
        return OpportunityIngestionPayload(
            opportunity=opportunity,
            evidence_items=list(self.evidence_items),
        )

    def create_specification(
        self,
        title: str,
        description: str,
        category: OpportunityCategory,
        audience: str,
        monetization_notes: str,
        estimated_revenue_min: Decimal = Decimal("0.00"),
        estimated_revenue_max: Decimal = Decimal("0.00"),
        estimated_cost_min: Decimal = Decimal("0.00"),
        estimated_cost_max: Decimal = Decimal("0.00"),
        production_difficulty: str = "",
        distribution_difficulty: str = "",
        automation_potential: str = "",
        platform_dependency: str = "",
        regulatory_notes: str = "",
        competition_level: str = "",
        growth_indicators: str = "",
        reviewer_notes: str = "",
    ) -> HumanOpportunitySpecification:
        """Create a human review specification referencing this candidate."""
        return HumanOpportunitySpecification(
            candidate_id=self.id,
            candidate=self,
            title=title,
            description=description,
            category=category,
            audience=audience,
            monetization_notes=monetization_notes,
            estimated_revenue_min=estimated_revenue_min,
            estimated_revenue_max=estimated_revenue_max,
            estimated_cost_min=estimated_cost_min,
            estimated_cost_max=estimated_cost_max,
            production_difficulty=production_difficulty,
            distribution_difficulty=distribution_difficulty,
            automation_potential=automation_potential,
            platform_dependency=platform_dependency,
            regulatory_notes=regulatory_notes,
            competition_level=competition_level,
            growth_indicators=growth_indicators,
            reviewer_notes=reviewer_notes,
        )


class HumanOpportunitySpecification(BaseModel):
    """Transient contract representing human review and business specification of a candidate.

    Bridges OpportunityCandidate to canonical Opportunity and OpportunityIngestionPayload.
    Guarantees that business models, audiences, monetization strategies, and economics
    are explicitly human-specified rather than hallucinated or inferred from research metrics.
    """

    candidate_id: UUID
    title: str
    description: str
    category: OpportunityCategory
    audience: str
    monetization_notes: str

    # Economics (explicitly human-supplied estimates)
    estimated_revenue_min: Decimal = Decimal("0.00")
    estimated_revenue_max: Decimal = Decimal("0.00")
    estimated_cost_min: Decimal = Decimal("0.00")
    estimated_cost_max: Decimal = Decimal("0.00")

    # Feasibility & operational dimensions (human-specified)
    production_difficulty: str = ""
    distribution_difficulty: str = ""
    automation_potential: str = ""
    platform_dependency: str = ""
    regulatory_notes: str = ""
    competition_level: str = ""
    growth_indicators: str = ""

    # Human review context
    reviewer_notes: str = ""

    # Retained candidate context (transient, in-memory)
    candidate: OpportunityCandidate | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("title", "description", "audience", "monetization_notes")
    @classmethod
    def required_text_fields_must_not_be_empty(cls, v: str) -> str:
        clean = v.strip() if v else ""
        if not clean:
            raise ValueError("Required business specification field must not be empty or whitespace.")
        return clean

    @field_validator("estimated_revenue_min", "estimated_revenue_max", "estimated_cost_min", "estimated_cost_max")
    @classmethod
    def economic_values_must_be_non_negative(cls, v: Decimal) -> Decimal:
        if v < Decimal("0.00"):
            raise ValueError("Economic estimates cannot be negative.")
        return v

    @model_validator(mode="after")
    def validate_economic_ranges(self) -> HumanOpportunitySpecification:
        if self.estimated_revenue_max < self.estimated_revenue_min:
            raise ValueError(
                f"estimated_revenue_max ({self.estimated_revenue_max}) cannot be less than "
                f"estimated_revenue_min ({self.estimated_revenue_min})."
            )
        if self.estimated_cost_max < self.estimated_cost_min:
            raise ValueError(
                f"estimated_cost_max ({self.estimated_cost_max}) cannot be less than "
                f"estimated_cost_min ({self.estimated_cost_min})."
            )
        return self

    def to_ingestion_payload(
        self,
        candidate: OpportunityCandidate | None = None,
    ) -> OpportunityIngestionPayload:
        """Convert this human specification and its linked candidate into an OpportunityIngestionPayload."""
        target_candidate = candidate or self.candidate
        if target_candidate is None:
            raise ValueError(
                "A valid OpportunityCandidate is required to produce an OpportunityIngestionPayload."
            )
        if target_candidate.id != self.candidate_id:
            raise ValueError(
                f"Candidate ID mismatch: specification references {self.candidate_id}, "
                f"but candidate provided has ID {target_candidate.id}."
            )

        trend_strength_val = (
            target_candidate.trend_status.value
            if target_candidate.trend_status
            else ""
        )

        spec_notes = ""
        if self.reviewer_notes and self.reviewer_notes.strip():
            spec_notes = f"[HUMAN_SPECIFICATION] Reviewer rationale: {self.reviewer_notes.strip()}"

        opp = Opportunity(
            title=self.title,
            description=self.description,
            category=self.category,
            status=OpportunityStatus.DISCOVERED,
            audience=self.audience,
            trend_strength=trend_strength_val,
            growth_indicators=self.growth_indicators,
            competition_level=self.competition_level,
            monetization_notes=self.monetization_notes,
            production_difficulty=self.production_difficulty,
            distribution_difficulty=self.distribution_difficulty,
            automation_potential=self.automation_potential,
            platform_dependency=self.platform_dependency,
            regulatory_notes=self.regulatory_notes,
            estimated_revenue_min=self.estimated_revenue_min,
            estimated_revenue_max=self.estimated_revenue_max,
            estimated_cost_min=self.estimated_cost_min,
            estimated_cost_max=self.estimated_cost_max,
            evidence_notes=spec_notes,
        )

        return OpportunityIngestionPayload(
            opportunity=opp,
            evidence_items=list(target_candidate.evidence_items),
        )


class OpportunityIngestionPayload(BaseModel):
    """Typed payload for ingesting research findings and constructing an Opportunity."""

    opportunity: Opportunity
    evidence_items: list[ResearchEvidenceItem] = Field(default_factory=list)


class OpportunityExperimentSummary(BaseModel):
    """Structured summary of an experiment linked to an opportunity.

    Captures only existing, supported dimensions:
    - experiment_id: unique identifier
    - channel: distribution channel from Experiment model
    - status: execution lifecycle status from Experiment model
    - actual_spend: authoritative ledger spend (EXPERIMENT_SPEND)
    """

    experiment_id: UUID
    channel: Channel
    status: ExperimentStatus
    actual_spend: Decimal = Decimal("0.00")


class OpportunityIntelligenceContext(BaseModel):
    """Synthesized, read-only intelligence context for an Opportunity.

    Combines canonical Opportunity data, deterministic evaluation,
    experiment history, authoritative ledger spend, recorded revenue/profit,
    and accumulated learnings into a single queryable record.
    """

    opportunity: Opportunity
    evaluation: OpportunityEvaluationResult
    total_experiments: int
    experiments_summary: list[OpportunityExperimentSummary] = Field(default_factory=list)

    total_actual_spend: Decimal = Decimal("0.00")
    total_measured_revenue: Decimal | None = None
    net_measured_profit_loss: Decimal | None = None

    accumulated_learnings: list[ExperimentLearning] = Field(default_factory=list)
    synthesized_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

