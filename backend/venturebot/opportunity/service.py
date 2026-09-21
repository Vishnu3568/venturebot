"""Opportunity Intelligence Service for VentureBot (Step 14).

Provides a deterministic, read-only synthesis layer that aggregates existing
data for an Opportunity across evaluation, experiments, authoritative ledger spend,
measured metrics, and accumulated learnings.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.learning import LearningRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.evaluation.evaluator import OpportunityEvaluator
from venturebot.opportunity.models import (
    OpportunityExperimentSummary,
    OpportunityIntelligenceContext,
)


class OpportunityIntelligenceService:
    """Read-only deterministic service synthesizing complete opportunity intelligence."""

    @classmethod
    def get_intelligence(
        cls,
        session: Session,
        opportunity_id: UUID,
    ) -> OpportunityIntelligenceContext:
        """Produce a complete, read-only intelligence synthesis for an Opportunity.

        Enforces:
        - Opportunity must exist in database (raises ValueError if not found).
        - Strictly read-only: creates 0 transactions, alters 0 balances, creates 0 records.
        - Deterministic evaluation using OpportunityEvaluator.
        - Authoritative actual spend calculated directly from CapitalRepository.
        - Measured revenue and profit/loss strictly from recorded metrics (no fake values).
        - Preserves accumulated learnings from LearningRepository.
        """
        opp_repo = OpportunityRepository(session, auto_commit=False)
        opp = opp_repo.get(opportunity_id)
        if opp is None:
            raise ValueError(f"Opportunity '{opportunity_id}' does not exist.")

        # 1. Deterministic evaluation
        evaluation = OpportunityEvaluator.evaluate(opp)

        # 2. Retrieve linked experiments
        exp_repo = ExperimentRepository(session, auto_commit=False)
        experiments = exp_repo.list(opportunity_id=opportunity_id)

        # 3. Retrieve authoritative actual spend from ledger and build summaries
        cap_repo = CapitalRepository(session, auto_commit=False)
        metrics_repo = MetricsRepository(session, auto_commit=False)

        experiments_summary: list[OpportunityExperimentSummary] = []
        total_actual_spend = Decimal("0.00")

        total_measured_revenue: Decimal | None = None
        net_measured_profit_loss: Decimal | None = None
        has_observed_revenue = False
        has_observed_profit_loss = False

        accumulated_revenue = Decimal("0.00")
        accumulated_profit_loss = Decimal("0.00")

        for exp in experiments:
            # Authoritative actual spend from capital transactions ledger
            actual_spend = cap_repo.get_experiment_actual_spend(exp.id)
            total_actual_spend += actual_spend

            experiments_summary.append(
                OpportunityExperimentSummary(
                    experiment_id=exp.id,
                    channel=exp.channel,
                    status=exp.status,
                    actual_spend=actual_spend,
                )
            )

            # Retrieve latest recorded metrics for this experiment
            latest_metrics = metrics_repo.get_latest_for_experiment(exp.id)
            if latest_metrics is not None:
                if latest_metrics.revenue is not None:
                    has_observed_revenue = True
                    accumulated_revenue += latest_metrics.revenue
                if latest_metrics.profit_loss is not None:
                    has_observed_profit_loss = True
                    accumulated_profit_loss += latest_metrics.profit_loss

        if has_observed_revenue:
            total_measured_revenue = accumulated_revenue
        if has_observed_profit_loss:
            net_measured_profit_loss = accumulated_profit_loss

        # 4. Retrieve accumulated learnings for this opportunity
        learning_repo = LearningRepository(session, auto_commit=False)
        accumulated_learnings = learning_repo.list_for_opportunity(opportunity_id)

        return OpportunityIntelligenceContext(
            opportunity=opp,
            evaluation=evaluation,
            total_experiments=len(experiments),
            experiments_summary=experiments_summary,
            total_actual_spend=total_actual_spend,
            total_measured_revenue=total_measured_revenue,
            net_measured_profit_loss=net_measured_profit_loss,
            accumulated_learnings=accumulated_learnings,
        )
