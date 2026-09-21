"""SQLAlchemy database models for VentureBot."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CapitalTransactionORM(Base):
    """Authoritative ledger record for all internal capital movements."""
    __tablename__ = "capital_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=2, asdecimal=True), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class OpportunityORM(Base):
    """Potentially monetizable opportunity discovered by VentureBot."""
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="discovered", index=True)

    # Evidence
    source: Mapped[str] = mapped_column(String(500), default="")
    evidence_notes: Mapped[str] = mapped_column(Text, default="")

    # Market signals
    audience: Mapped[str] = mapped_column(String(500), default="")
    trend_strength: Mapped[str] = mapped_column(String(100), default="")
    growth_indicators: Mapped[str] = mapped_column(Text, default="")
    competition_level: Mapped[str] = mapped_column(String(100), default="")

    # Feasibility
    monetization_notes: Mapped[str] = mapped_column(Text, default="")
    production_difficulty: Mapped[str] = mapped_column(String(100), default="")
    distribution_difficulty: Mapped[str] = mapped_column(String(100), default="")
    automation_potential: Mapped[str] = mapped_column(String(100), default="")
    platform_dependency: Mapped[str] = mapped_column(String(255), default="")
    regulatory_notes: Mapped[str] = mapped_column(Text, default="")

    # Economics (estimates)
    estimated_revenue_min: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )
    estimated_revenue_max: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )
    estimated_cost_min: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )
    estimated_cost_max: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    experiments: Mapped[list[ExperimentORM]] = relationship(
        "ExperimentORM", back_populates="opportunity", cascade="all, delete-orphan"
    )
    decisions: Mapped[list[DecisionORM]] = relationship(
        "DecisionORM",
        back_populates="opportunity",
        foreign_keys="DecisionORM.opportunity_id",
    )
    learnings: Mapped[list[ExperimentLearningORM]] = relationship(
        "ExperimentLearningORM",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )



class ExperimentORM(Base):
    """Controlled test derived from an Opportunity."""
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )

    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    monetization_method: Mapped[str] = mapped_column(String(50), nullable=False)

    # Budget ceilings
    allocated_budget: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=False
    )
    max_allowed_spend: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=False
    )
    actual_spend: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )

    success_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    failure_criteria: Mapped[str] = mapped_column(Text, nullable=False)

    # Timeline
    planned_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    opportunity: Mapped[OpportunityORM] = relationship("OpportunityORM", back_populates="experiments")
    metrics: Mapped[list[ExperimentMetricsORM]] = relationship(
        "ExperimentMetricsORM", back_populates="experiment", cascade="all, delete-orphan"
    )
    decisions: Mapped[list[DecisionORM]] = relationship(
        "DecisionORM",
        back_populates="experiment",
        foreign_keys="DecisionORM.experiment_id",
    )
    learnings: Mapped[list[ExperimentLearningORM]] = relationship(
        "ExperimentLearningORM",
        back_populates="experiment",
        cascade="all, delete-orphan",
    )
    external_execution: Mapped[ExternalExecutionORM | None] = relationship(
        "ExternalExecutionORM",
        back_populates="experiment",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def remaining_budget(self) -> Decimal:
        """Remaining allocated funds available for this experiment."""
        return max(Decimal("0.00"), self.allocated_budget - self.actual_spend)




class ExperimentMetricsORM(Base):
    """Measurable results recorded for an Experiment."""
    __tablename__ = "experiment_metrics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False, default="fact", index=True)
    source_reference: Mapped[str] = mapped_column(Text, default="")

    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    visitors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    revenue: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True, default=None
    )
    cost: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), default=Decimal("0.00")
    )
    profit_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True, default=None
    )

    roas: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    roi: Mapped[float | None] = mapped_column(Float, nullable=True)

    retention_notes: Mapped[str] = mapped_column(Text, default="")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    experiment: Mapped[ExperimentORM] = relationship("ExperimentORM", back_populates="metrics")


class DecisionORM(Base):
    """Recorded decision regarding an Opportunity or Experiment."""
    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="SET NULL"), nullable=True, index=True
    )

    outcome: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    opportunity: Mapped[OpportunityORM | None] = relationship(
        "OpportunityORM", back_populates="decisions", foreign_keys=[opportunity_id]
    )
    experiment: Mapped[ExperimentORM | None] = relationship(
        "ExperimentORM", back_populates="decisions", foreign_keys=[experiment_id]
    )
    learnings: Mapped[list[ExperimentLearningORM]] = relationship(
        "ExperimentLearningORM",
        back_populates="decision",
        foreign_keys="ExperimentLearningORM.decision_id",
    )


class ExperimentLearningORM(Base):
    """Structured retrospective learning recorded for an Experiment."""
    __tablename__ = "experiment_learnings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    what_worked: Mapped[str] = mapped_column(Text, default="[]")
    what_failed: Mapped[str] = mapped_column(Text, default="[]")
    key_learnings: Mapped[str] = mapped_column(Text, default="[]")
    future_hypotheses: Mapped[str] = mapped_column(Text, default="[]")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    experiment: Mapped[ExperimentORM] = relationship("ExperimentORM", back_populates="learnings")
    opportunity: Mapped[OpportunityORM] = relationship("OpportunityORM", back_populates="learnings")
    decision: Mapped[DecisionORM | None] = relationship(
        "DecisionORM",
        back_populates="learnings",
        foreign_keys=[decision_id],
    )


class ExternalExecutionORM(Base):
    """Authoritative record of the current external deployment for an Experiment."""
    __tablename__ = "external_executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False, default="meta")
    external_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    adset_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    creative_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    image_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    experiment: Mapped[ExperimentORM] = relationship("ExperimentORM", back_populates="external_execution")

