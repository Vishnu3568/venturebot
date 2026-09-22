"""Experiment Outcome Decision Foundation Service for VentureBot.

Governs recording and querying explicit, human/authorized outcome decisions
(SCALE, ITERATE, KILL, HOLD, APPROVE) based on recorded experiment evidence.
Provides an immutable audit trail without automated decisions or financial side-effects.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM, OpportunityORM
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.models.decision import Decision, DecisionOutcome


class ExperimentDecisionService:
    """Service governing explicit experiment outcome decision recording."""

    @classmethod
    def record_decision(
        cls,
        session: Session,
        decision: Decision,
        auto_commit: bool = True,
    ) -> Decision:
        """Record an explicit outcome decision for an Experiment or Opportunity.

        Enforces:
        - Decision outcome must be an explicit DecisionOutcome (SCALE, ITERATE, KILL, HOLD, APPROVE).
        - Rationale (reason) is mandatory and non-empty.
        - Experiment and/or Opportunity reference must exist (no orphan decisions).
        - Auto-resolves Opportunity reference if Experiment is provided.
        - Decisions are append-only and immutable.
        - Zero financial ledger side-effects (0 capital transactions, 0 spend mutations).
        - Zero automated algorithmic decision-making (no automated threshold rules).
        """
        clean_reason = decision.reason.strip() if decision.reason else ""
        if not clean_reason:
            raise ValueError("Decision requires an explicit, non-empty reason.")

        if decision.experiment_id is None and decision.opportunity_id is None:
            raise ValueError("Decision must be associated with an experiment_id or opportunity_id.")

        opp_id = decision.opportunity_id

        if decision.experiment_id is not None:
            exp_orm = session.get(ExperimentORM, decision.experiment_id)
            if exp_orm is None:
                raise ValueError(f"Experiment '{decision.experiment_id}' does not exist.")
            if opp_id is None:
                opp_id = exp_orm.opportunity_id
            elif opp_id != exp_orm.opportunity_id:
                raise ValueError(
                    f"Decision opportunity_id '{opp_id}' does not match experiment opportunity_id '{exp_orm.opportunity_id}'."
                )

        if decision.experiment_id is None and opp_id is not None:
            opp_orm = session.get(OpportunityORM, opp_id)
            if opp_orm is None:
                raise ValueError(f"Opportunity '{opp_id}' does not exist.")

        outcome_val = (
            decision.outcome
            if isinstance(decision.outcome, DecisionOutcome)
            else DecisionOutcome(str(decision.outcome))
        )

        validated_decision = Decision(
            id=decision.id,
            opportunity_id=opp_id,
            experiment_id=decision.experiment_id,
            outcome=outcome_val,
            reason=clean_reason,
            evidence_summary=decision.evidence_summary.strip() if decision.evidence_summary else "",
            confidence=decision.confidence,
            decided_at=decision.decided_at,
        )

        repo = DecisionRepository(session, auto_commit=auto_commit)
        return repo.create(validated_decision)

    @classmethod
    def get_decision(
        cls,
        session: Session,
        decision_id: UUID,
    ) -> Decision | None:
        """Retrieve a specific decision by ID."""
        repo = DecisionRepository(session, auto_commit=False)
        return repo.get(decision_id)

    @classmethod
    def list_decisions_for_experiment(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> list[Decision]:
        """List all decisions recorded for an experiment in reverse chronological order."""
        repo = DecisionRepository(session, auto_commit=False)
        return repo.list(experiment_id=experiment_id)

    @classmethod
    def list_decisions_for_opportunity(
        cls,
        session: Session,
        opportunity_id: UUID,
    ) -> list[Decision]:
        """List all decisions recorded for an opportunity in reverse chronological order."""
        repo = DecisionRepository(session, auto_commit=False)
        return repo.list(opportunity_id=opportunity_id)

    @classmethod
    def get_latest_decision_for_experiment(
        cls,
        session: Session,
        experiment_id: UUID,
    ) -> Decision | None:
        """Retrieve the most recently recorded decision for an experiment."""
        repo = DecisionRepository(session, auto_commit=False)
        return repo.get_latest_for_experiment(experiment_id)

    @classmethod
    def get_latest_decision_for_opportunity(
        cls,
        session: Session,
        opportunity_id: UUID,
    ) -> Decision | None:
        """Retrieve the most recently recorded decision for an opportunity."""
        repo = DecisionRepository(session, auto_commit=False)
        return repo.get_latest_for_opportunity(opportunity_id)
